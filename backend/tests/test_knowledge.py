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

from backend.app import knowledge, knowledge_runs, wiki_artifacts
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RunRecord,
)


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
            archive_dir=self.archive,
            runtime_dir=self.runtime,
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
                "runs": connection.execute(
                    "SELECT run_id,ticket,provider,model,role,spawned_at,ended_at,outcome FROM runs ORDER BY run_id"
                ).fetchall(),
                "events": connection.execute(
                    "SELECT run_id,seq,type,ts,text_excerpt FROM events ORDER BY run_id,seq"
                ).fetchall(),
            }

    def test_rebuild_is_idempotent(self) -> None:
        _write_note(self.vault / "one.md", "One", "idempotent corpus [[two]]")
        _write_note(self.vault / "two.md", "Two", "second note")
        run_dir = self.archive / "WIKI-100" / "20260714-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "00000000-0000-4000-8000-000000000100",
                    "agent_id": "WIKI-100",
                    "provider": "codex",
                    "model": "fixture",
                    "role": "implement",
                    "created_at": "2026-07-14T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "ended_at": "2026-07-14T12:05:00+00:00",
                    "outcome": "merged",
                    "source": "headless-supervisor",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "events.jsonl").write_text(
            json.dumps(
                {
                    "seq": 1,
                    "kind": "agent_message",
                    "normalized_at": "2026-07-14T12:01:00+00:00",
                    "payload": {"text": "idempotent fleet event"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.index.rebuild()
        first = self._table_snapshot()
        first_counts = self.index.row_counts()
        self.index.rebuild()
        self.assertEqual(self.index.row_counts(), first_counts)
        self.assertEqual(self._table_snapshot(), first)

    def test_malformed_event_lines_are_counted_and_skipped(self) -> None:
        run_dir = self.archive / "WIKI-100" / "20260714-130000"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "00000000-0000-4000-8000-000000000101",
                    "agent_id": "WIKI-100",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "events.jsonl").write_text(
            "not-json\n"
            + json.dumps(
                {"seq": 1, "kind": "message", "payload": {"text": "validneedle"}}
            )
            + "\n{\"broken\":\n",
            encoding="utf-8",
        )
        stats = self.index.index_run_directory(run_dir)
        self.assertEqual(stats.malformed_event_lines, 2)
        self.assertEqual(stats.events_indexed, 1)
        self.assertEqual(self.index.search("validneedle")["results"][0]["seq"], 1)

    def test_event_chunks_are_excerpted_at_unicode_boundaries(self) -> None:
        prefix = "agent_message\n"
        text = "🙂" * (knowledge_runs.EVENT_CHUNK_EXCERPT_MAX_CHARS - len(prefix) + 1)
        events_path = _write_run(
            self.archive / "WIKI-102" / "20260714-131000",
            run_id="00000000-0000-4000-8000-000000000110",
            ticket="WIKI-102",
            text=text,
        )
        stats = self.index.index_run_directory(events_path.parent)
        with closing(sqlite3.connect(self.db)) as connection:
            stored = connection.execute(
                "SELECT text FROM chunks WHERE source_kind = 'event'"
            ).fetchone()[0]
        self.assertEqual(len(stored), knowledge_runs.EVENT_CHUNK_EXCERPT_MAX_CHARS)
        self.assertEqual(stored, prefix + text[: len(text) - 1])
        self.assertEqual(stored.encode("utf-8").decode("utf-8"), stored)
        self.assertEqual(stats.event_chunks_excerpted, 1)

    def test_ingest_filters_count_tool_base64_and_ansi_events(self) -> None:
        cases = (
            (
                "item_commandExecution_outputDelta",
                "toolneedle " + "x" * knowledge_runs.TOOL_OUTPUT_MAX_CHARS,
                "tool_outputs_truncated",
                "toolneedle",
            ),
            (
                "agent_message",
                "QUJD" * (knowledge_runs.BASE64_BLOB_MIN_CHARS // 4 + 1),
                "base64_blobs_skipped",
                None,
            ),
            (
                "codex_stderr",
                "\x1b[31m" * 20 + "legacy pane redraw",
                "ansi_heavy_lines_skipped",
                None,
            ),
        )
        for index, (event_type, text, counter, searchable) in enumerate(cases, start=1):
            run_dir = self.archive / f"filter-{index}" / "20260714-132000"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"run_id": f"filter-{index}", "agent_id": "WIKI-102"}),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps({"seq": 1, "kind": event_type, "payload": {"text": text}})
                + "\n",
                encoding="utf-8",
            )
            stats = self.index.index_run_directory(run_dir)
            self.assertEqual(getattr(stats, counter), 1)
            if searchable:
                self.assertEqual(
                    self.index.search(searchable, kind="run")["results"][0]["run_id"],
                    f"filter-{index}",
                )
            else:
                with closing(sqlite3.connect(self.db)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM chunks WHERE source_kind = 'event' AND source_id = ?",
                            (f"filter-{index}",),
                        ).fetchone()[0],
                        0,
                    )

    def test_live_runs_are_indexed_lazily_and_delta_by_sequence(self) -> None:
        self.index.rebuild()
        run_id = "00000000-0000-4000-8000-000000000102"
        run_dir = self.runtime / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "agent_id": "WIKI-102",
                    "provider": "claude",
                    "model": "fixture",
                    "role": "implement",
                    "created_at": "2026-07-14T14:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        events_path = run_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "kind": "claude_assistant",
                    "normalized_at": "2026-07-14T14:01:00+00:00",
                    "payload": {"text": "livelazyneedle first"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        first = self.index.search(
            "livelazyneedle",
            ticket="WIKI-102",
            kind="run",
            event_type="claude_assistant",
            since="2026-07-14",
        )["results"]
        self.assertEqual(first[0]["citation"], f"{run_id}:1")

        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "seq": 2,
                        "kind": "claude_user",
                        "normalized_at": "2026-07-14T14:02:00+00:00",
                        "payload": {"text": "seconddeltaneedle"},
                    }
                )
                + "\n"
            )
        second = self.index.search("seconddeltaneedle", kind="run")["results"]
        self.assertEqual(second[0]["seq"], 2)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
                ).fetchone()[0],
                2,
            )

    def test_schema_mismatch_and_corruption_drop_to_partial_then_rebuild(self) -> None:
        _write_note(self.vault / "recovery.md", "Recovery", "recoveryneedle")
        self.index.rebuild()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE meta SET schema_version = 999")
            connection.commit()
        partial = self.index.search("recoveryneedle")
        self.assertTrue(partial["rebuilding"])
        self.assertEqual(partial["results"], [])
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
                archive_dir=self.archive,
                runtime_dir=self.runtime,
            )
        )
        with self.assertRaisesRegex(knowledge.KnowledgeUnavailable, "cannot open"):
            broken.search("anything")

    def test_unreadable_live_run_returns_existing_results_as_stale(self) -> None:
        _write_note(self.vault / "durable.md", "Durable", "resilientsearchneedle")
        self.index.rebuild()
        _write_run(
            self.runtime / "runs" / "unreadable-live-run",
            run_id="00000000-0000-4000-8000-000000000103",
            ticket="WIKI-103",
            text="live source text",
        )
        with mock.patch.object(
            knowledge,
            "read_run_events",
            side_effect=PermissionError("fixture is unreadable"),
        ):
            result = self.index.search("resilientsearchneedle")
        self.assertTrue(result["stale"])
        self.assertEqual(result["results"][0]["citation"], "durable.md")

    def test_locked_live_run_writer_returns_existing_results_as_stale(self) -> None:
        _write_note(self.vault / "locked.md", "Locked", "lockedsearchneedle")
        self.index.rebuild()
        _write_run(
            self.runtime / "runs" / "locked-live-run",
            run_id="00000000-0000-4000-8000-000000000104",
            ticket="WIKI-104",
            text="new live text",
        )
        with closing(sqlite3.connect(self.db)) as blocker:
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")
            result = self.index.search("lockedsearchneedle")
            blocker.rollback()
        self.assertTrue(result["stale"])
        self.assertEqual(result["results"][0]["citation"], "locked.md")

    def test_bad_archive_is_skipped_without_blocking_good_archive(self) -> None:
        bad_events = _write_run(
            self.archive / "bad" / "20260714-100000",
            run_id="00000000-0000-4000-8000-000000000105",
            ticket="WIKI-105",
            text="unreadable archive text",
        )
        _write_run(
            self.archive / "good" / "20260714-110000",
            run_id="00000000-0000-4000-8000-000000000106",
            ticket="WIKI-106",
            text="goodarchiveneedle",
        )
        read_run_events = knowledge.read_run_events

        def read_fixture(path: Path, *, after_seq: int):
            if path == bad_events:
                raise PermissionError("fixture is unreadable")
            return read_run_events(path, after_seq=after_seq)

        self.index.request_refresh()
        with mock.patch.object(knowledge, "read_run_events", side_effect=read_fixture):
            stats = self.index.refresh_all()
        self.assertEqual(stats.runs_skipped, 1)
        self.assertEqual(stats.runs_indexed, 1)
        self.assertFalse(self.index.rebuild_marker.exists())
        self.assertFalse(self.index.refresh_marker.exists())
        result = self.index.search("goodarchiveneedle")
        self.assertFalse(result["stale"])
        self.assertEqual(result["results"][0]["run_id"], "00000000-0000-4000-8000-000000000106")

    def test_archive_hook_enqueues_real_store_path_under_one_second(self) -> None:
        _write_note(
            self.vault / "diagnosis.md",
            "Modal Diagnosis",
            "sharedcometneedle from the durable note",
        )
        self.index.rebuild()
        store_paths = RuntimePaths(
            runtime_dir=self.runtime,
            socket_path=self.runtime / "supervisor.sock",
            registry_path=self.root / "agent-registry.json",
            archive_dir=self.archive,
            status_dir=self.root / "status",
        )
        worktree = self.root / "worktree"
        worktree.mkdir()
        store = RunStore(store_paths)
        record = store.create(
            RunRecord.new(
                agent_id="WIKI-100",
                provider=ProviderKind.CODEX,
                role="implement",
                model="fixture",
                worktree=str(worktree),
                prompt="fixture prompt",
            )
        )
        raw = store.append_raw(
            record.run_id,
            provider="codex",
            direction="server",
            payload={"method": "item/completed"},
        )
        store.append_normalized(
            record.run_id,
            raw_seq=raw["seq"],
            disposition=EventDisposition.RENDERED,
            kind="agent_message",
            payload={"text": "sharedcometneedle from the archived fleet run"},
        )
        store.transition(record.run_id, LifecycleState.COMPLETED)
        with mock.patch.dict(
            os.environ,
            {
                "WIKI_KNOWLEDGE_DB_PATH": str(self.db),
                "WIKI_VAULT_DIR": str(self.vault),
                "WIKI_AGENT_RUNTIME_DIR": str(self.runtime),
                "WIKI_AGENT_ARCHIVE_DIR": str(self.archive),
            },
        ):
            started = time.perf_counter()
            archived, session_dir = store.archive_current(
                record.run_id, outcome="merged"
            )
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue((session_dir / "events.jsonl").is_file())
        self.assertEqual(archived.outcome, "merged")
        self.assertTrue(self.index.refresh_marker.is_file())
        self.index.refresh_all()
        results = self.index.search("sharedcometneedle")["results"]
        self.assertEqual({row["kind"] for row in results}, {"note", "run"})
        note_hit = next(row for row in results if row["kind"] == "note")
        run_hit = next(row for row in results if row["kind"] == "run")
        self.assertEqual(note_hit["citation"], "diagnosis.md")
        self.assertEqual(run_hit["run_id"], record.run_id)
        self.assertEqual(run_hit["seq"], 1)

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
        self.index.search("budgetneedle", limit=10)  # warm lazy-run + page caches
        timings = []
        for _ in range(100):
            query_started = time.perf_counter()
            self.index.search("budgetneedle", limit=10)
            timings.append(time.perf_counter() - query_started)
        timings.sort()
        p95 = timings[int(len(timings) * 0.95) - 1]
        self.assertLess(p95, 0.05, f"query p95 was {p95 * 1000:.2f}ms")

    def test_fixture_index_size_is_bounded_relative_to_source_events(self) -> None:
        event_paths = []
        for index in range(24):
            event_paths.append(
                _write_run(
                    self.archive / f"size-{index}" / "20260714-140000",
                    run_id=f"size-{index}",
                    ticket="WIKI-102",
                    text=f"fixture diet needle {index} " + "x" * (64 * 1024),
                )
            )
        input_bytes = sum(path.stat().st_size for path in event_paths)
        stats = self.index.rebuild()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(stats.event_chunks_excerpted, len(event_paths))
        self.assertLess(
            self.db.stat().st_size,
            input_bytes // 4,
            "excerpt-only chunks should stay well below the source transcript size",
        )
        self.assertEqual(
            self.index.search("fixture diet needle", kind="run")["results"][0]["ticket"],
            "WIKI-102",
        )

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
    def test_cli_legacy_archive_sweep_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            runtime = root / "runtime"
            archive = root / "archive"
            vault.mkdir()
            runtime.mkdir()
            archive.mkdir()
            _write_run(
                archive / "supervisor" / "20260714-150000",
                run_id="supervisor-run",
                ticket="WIKI-102",
                text="supervisorarchiveneedle",
            )
            _write_run(
                archive / "legacy" / "20260714-150000",
                run_id="legacy-run",
                ticket="WIKI-102",
                text="legacyarchiveneedle",
                archive_source=None,
            )
            env = {
                **os.environ,
                "WIKI_VAULT_DIR": str(vault),
                "WIKI_KNOWLEDGE_DB_PATH": str(root / "knowledge.db"),
                "WIKI_AGENT_RUNTIME_DIR": str(runtime),
                "WIKI_AGENT_ARCHIVE_DIR": str(archive),
            }
            default = subprocess.run(
                [sys.executable, str(WIKI_CLI), "index", "rebuild"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(default.returncode, 0, msg=default.stderr)
            self.assertEqual(json.loads(default.stdout)["legacy_runs_skipped"], 1)
            included = subprocess.run(
                [sys.executable, str(WIKI_CLI), "index", "rebuild", "--include-legacy"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(included.returncode, 0, msg=included.stderr)
            self.assertEqual(json.loads(included.stdout)["events_indexed"], 2)
            search = subprocess.run(
                [sys.executable, str(WIKI_CLI), "search", "legacyarchiveneedle", "--json"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(search.returncode, 0, msg=search.stderr)
            self.assertEqual(json.loads(search.stdout)["results"][0]["run_id"], "legacy-run")

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
