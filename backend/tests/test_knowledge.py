"""WIKI-100 knowledge index tests; every mutable path is temporary."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend.app import knowledge, wiki_artifacts
from backend.app.agent_runtime.archive_protocol import commit_archive

REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_CLI = REPO_ROOT / "wiki"

def _write_note(path: Path, title: str, body: str, *, note_type: str = "reference") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"type: {note_type}\n"
        "tags: [fixture]\n"
        "created: 2026-07-14\n"
        "updated: 2026-07-14\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n",
        encoding="utf-8",
    )

def _write_run(
    run_dir: Path,
    *,
    run_id: str,
    ticket: str,
    text: str,
    archive_source: str | None = "headless-supervisor",
) -> Path:
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "agent_id": ticket, "provider": "codex"}),
        encoding="utf-8",
    )
    if archive_source is not None:
        (run_dir / "meta.json").write_text(
            json.dumps({"source": archive_source}), encoding="utf-8"
        )
    events_path = run_dir / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "seq": 1,
                "kind": "agent_message",
                "normalized_at": "2026-07-14T12:00:00+00:00",
                "payload": {"text": text},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "raw.jsonl").write_text("", encoding="utf-8")
    commit_archive(run_dir)
    return events_path

class ChunkerAndResolverTests(unittest.TestCase):
    def test_chunks_stay_heading_bounded(self) -> None:
        chunks = knowledge.chunk_markdown(
            "# Alpha\n" + "alpha words " * 20 + "\n## Beta\n" + "beta words " * 20,
            max_chars=80,
        )
        self.assertGreater(len(chunks), 2)
        self.assertEqual({chunk.heading for chunk in chunks}, {"Alpha", "Beta"})
        for chunk in chunks:
            self.assertFalse("alpha" in chunk.text and "beta" in chunk.text)

    def test_fenced_blocks_are_never_split_or_treated_as_headings(self) -> None:
        fence = "```python\n# not a heading\n" + "print('fence')\n" * 12 + "```"
        chunks = knowledge.chunk_markdown(
            f"# Real heading\nintro text\n\n{fence}\n\noutro text",
            max_chars=64,
        )
        fence_chunks = [chunk for chunk in chunks if "```python" in chunk.text]
        self.assertEqual(len(fence_chunks), 1)
        self.assertIn(fence, fence_chunks[0].text)
        self.assertEqual(fence_chunks[0].heading, "Real heading")

    def test_wikilink_resolution_handles_paths_anchors_code_and_ambiguity(self) -> None:
        paths = ["tools/target.md", "one/duplicate.md", "two/duplicate.md"]
        self.assertEqual(
            knowledge.resolve_wikilink_target("tools/target#Section", paths),
            "tools/target.md",
        )
        self.assertEqual(
            knowledge.resolve_wikilink_target("target", paths), "tools/target.md"
        )
        self.assertIsNone(knowledge.resolve_wikilink_target("duplicate", paths))
        links = knowledge.extract_wikilinks(
            "[[target|alias]]\n`[[inline]]`\n```md\n[[fenced]]\n```"
        )
        self.assertEqual(links, ["target"])

class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.archive = self.root / "archive"
        self.runtime = self.root / "runtime"
        self.db = self.root / "knowledge.db"
        for path in (self.vault, self.archive, self.runtime):
            path.mkdir(parents=True)
        self.paths = knowledge.KnowledgePaths(
            db_path=self.db,
            vault_dir=self.vault,
        )
        self.index = knowledge.KnowledgeIndex(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_delta_scan_uses_mtime_and_content_hash(self) -> None:
        note = self.vault / "tools" / "delta.md"
        _write_note(note, "Delta", "first searchable version")
        first = self.index.rebuild()
        self.assertEqual(first.notes_indexed, 1)
        with closing(sqlite3.connect(self.db)) as connection:
            original_ids = connection.execute(
                "SELECT id FROM chunks WHERE source_id = 'tools/delta.md' ORDER BY id"
            ).fetchall()

        stat = note.stat()
        os.utime(note, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        metadata_only = self.index.scan_vault()
        self.assertEqual(metadata_only.notes_indexed, 0)
        self.assertEqual(metadata_only.notes_metadata_updated, 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id FROM chunks WHERE source_id = 'tools/delta.md' ORDER BY id"
                ).fetchall(),
                original_ids,
            )

        preserved_mtime = note.stat().st_mtime_ns
        _write_note(note, "Delta", "second rarehashneedle version")
        os.utime(note, ns=(preserved_mtime, preserved_mtime))
        changed = self.index.scan_vault()
        self.assertEqual(changed.notes_indexed, 1)
        hits = self.index.search("rarehashneedle", kind="note")
        self.assertEqual(hits["results"][0]["path"], "tools/delta.md")

    def test_fts_ranking_and_link_graph_queries(self) -> None:
        _write_note(
            self.vault / "sqlite-handbook.md",
            "SQLite Handbook",
            "porter ranking target [[linked]] and [[missing-note]]",
        )
        _write_note(
            self.vault / "linked.md",
            "Linked",
            "A general note that mentions sqlite once.",
        )
        self.index.rebuild()
        results = self.index.search("sqlite", kind="note")["results"]
        self.assertEqual(results[0]["path"], "sqlite-handbook.md")
        self.assertEqual(
            self.index.backlinks("linked")["backlinks"], ["sqlite-handbook.md"]
        )
        self.assertEqual(
            self.index.unresolved()["unresolved"],
            [{"src_note": "sqlite-handbook.md", "dst_name": "missing-note"}],
        )
        self.assertIn("sqlite-handbook.md", self.index.orphans()["orphans"])

    def _table_snapshot(self) -> dict[str, list[tuple]]:
        with closing(sqlite3.connect(self.db)) as connection:
            return {
                "notes": connection.execute(
                    "SELECT path,title,type,tags,created,updated,mtime,content_hash FROM notes ORDER BY path"
                ).fetchall(),
                "chunks": connection.execute(
                    "SELECT id,source_kind,source_id,ticket,title,heading,text,pos FROM chunks ORDER BY id"
                ).fetchall(),
                "links": connection.execute(
                    "SELECT src_note,dst_name,resolved_path FROM links ORDER BY src_note,dst_name"
                ).fetchall(),
            }

    def test_rebuild_is_idempotent(self) -> None:
        _write_note(self.vault / "one.md", "One", "idempotent corpus [[two]]")
        _write_note(self.vault / "two.md", "Two", "second note")
        self.index.rebuild()
        first = self._table_snapshot()
        first_counts = self.index.row_counts()
        self.index.rebuild()
        self.assertEqual(self.index.row_counts(), first_counts)
        self.assertEqual(self._table_snapshot(), first)

    def test_schema_mismatch_and_corruption_drop_to_partial_then_rebuild(self) -> None:
        _write_note(self.vault / "recovery.md", "Recovery", "recoveryneedle")
        self.index.rebuild()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE meta SET schema_version = 999")
            connection.execute("CREATE TABLE obsolete(data BLOB)")
            connection.execute("INSERT INTO obsolete VALUES (zeroblob(2000000))")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertGreater(self.db.stat().st_size, 1_000_000)
        partial = self.index.search("recoveryneedle")
        self.assertTrue(partial["rebuilding"])
        self.assertEqual(partial["results"], [])
        self.assertLess(self.db.stat().st_size, 1_000_000)
        self.index.refresh_all()
        self.assertEqual(
            self.index.search("recoveryneedle")["results"][0]["path"],
            "recovery.md",
        )

        self.db.write_bytes(b"this is not sqlite")
        corrupt_partial = self.index.search("recoveryneedle")
        self.assertTrue(corrupt_partial["rebuilding"])
        self.assertTrue(corrupt_partial["stale"])
        self.assertEqual(corrupt_partial["results"], [])
        self.assertTrue(list(self.root.glob("knowledge.db.corrupt-*")))
        self.index.refresh_all()
        self.assertEqual(
            self.index.search("recoveryneedle")["results"][0]["path"],
            "recovery.md",
        )

    def test_unavailable_database_is_an_explicit_error(self) -> None:
        directory_as_db = self.root / "not-a-database"
        directory_as_db.mkdir()
        broken = knowledge.KnowledgeIndex(
            knowledge.KnowledgePaths(
                db_path=directory_as_db,
                vault_dir=self.vault,
            )
        )
        with self.assertRaisesRegex(knowledge.KnowledgeUnavailable, "cannot open"):
            broken.search("anything")

    def test_fixture_rebuild_and_query_budgets(self) -> None:
        for index in range(120):
            _write_note(
                self.vault / "corpus" / f"note-{index:03}.md",
                f"Fixture {index}",
                f"budgetneedle corpus row {index} with enough stable fixture text",
            )
        started = time.perf_counter()
        stats = self.index.rebuild()
        self.assertLess(time.perf_counter() - started, 60.0)
        self.assertEqual(stats.notes_indexed, 120)
        self.index.search("budgetneedle", limit=10)  # warm page caches
        timings = []
        for _ in range(100):
            query_started = time.perf_counter()
            self.index.search("budgetneedle", limit=10)
            timings.append(time.perf_counter() - query_started)
        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        self.assertLess(p95, 0.05, f"query p95 was {p95 * 1000:.2f}ms")

    def test_rebuild_indexes_notes_without_archive_transcripts(self) -> None:
        _write_note(self.vault / "notes.md", "Notes", "notesearchneedle")
        _write_run(
            self.archive / "WIKI-100" / "20260714-120000",
            run_id="archive-run",
            ticket="WIKI-100",
            text="archiveseachneedle " + "x" * (1024 * 1024),
        )
        stats = self.index.rebuild()
        self.assertEqual(stats.notes_indexed, 1)
        self.assertEqual(len(self.index.search("notesearchneedle")["results"]), 1)
        self.assertEqual(self.index.search("archiveseachneedle")["results"], [])
        with closing(sqlite3.connect(self.db)) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("events", tables)
        self.assertNotIn("runs", tables)
        self.assertLess(self.db.stat().st_size, 1024 * 1024)

    def test_mcp_search_tool_returns_same_structured_results(self) -> None:
        _write_note(self.vault / "mcp.md", "MCP", "mcpsearchneedle")
        self.index.rebuild()
        with mock.patch.dict(
            os.environ,
            {
                "WIKI_KNOWLEDGE_DB_PATH": str(self.db),
                "WIKI_VAULT_DIR": str(self.vault),
                "WIKI_AGENT_RUNTIME_DIR": str(self.runtime),
                "WIKI_AGENT_ARCHIVE_DIR": str(self.archive),
            },
        ):
            response = wiki_artifacts._response(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge",
                        "arguments": {"query": "mcpsearchneedle", "kind": "note"},
                    },
                }
            )
        assert response is not None
        result = response["result"]
        self.assertNotIn("isError", result)
        self.assertEqual(
            result["structuredContent"]["results"][0]["citation"], "mcp.md"
        )

class KnowledgeCliIsolationTests(unittest.TestCase):
    def test_cli_rebuild_search_links_and_post_op_enqueue_use_only_temp_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            runtime = root / "runtime"
            archive = root / "archive"
            vault.mkdir()
            runtime.mkdir()
            archive.mkdir()
            (vault / "map.md").write_text("# Map\n\n## Tools\n\n## Families\n", encoding="utf-8")
            _write_note(vault / "target.md", "Target", "clisearchneedle")
            _write_note(vault / "source.md", "Source", "[[target]]")
            db = root / "knowledge.db"
            env = {
                **os.environ,
                "HOME": str(root / "home"),
                "WIKI_VAULT_DIR": str(vault),
                "WIKI_KNOWLEDGE_DB_PATH": str(db),
                "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                "WIKI_AGENT_ARCHIVE_DIR": str(archive),
                "WIKI_AGENT_REGISTRY_PATH": str(root / "agent-registry.json"),
                "WIKI_AGENT_STATUS_DIR": str(root / "status"),
                "WIKI_AGENT_TMP_DIR": str(root / "agent-tmp"),
            }

            rebuild = subprocess.run(
                [sys.executable, str(WIKI_CLI), "index", "rebuild"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(rebuild.returncode, 0, msg=rebuild.stderr)
            search = subprocess.run(
                [
                    sys.executable,
                    str(WIKI_CLI),
                    "search",
                    "clisearchneedle",
                    "--kind",
                    "note",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(search.returncode, 0, msg=search.stderr)
            self.assertEqual(json.loads(search.stdout)["results"][0]["path"], "target.md")
            backlinks = subprocess.run(
                [sys.executable, str(WIKI_CLI), "links", "backlinks", "target"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(backlinks.returncode, 0, msg=backlinks.stderr)
            self.assertEqual(backlinks.stdout.strip(), "source.md")

            db.unlink()
            for suffix in ("-wal", "-shm", ".refresh", ".rebuilding"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)
            created = subprocess.run(
                [
                    sys.executable,
                    str(WIKI_CLI),
                    "note",
                    "new",
                    "tools/enqueued",
                    "--type",
                    "reference",
                    "--hook",
                    "enqueue fixture",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(created.returncode, 0, msg=created.stderr)
            self.assertFalse(db.exists(), "transactional CLI op must not index inline")
            self.assertTrue(Path(f"{db}.refresh").exists())

if __name__ == "__main__":
    unittest.main()
