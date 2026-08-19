from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

from backend.app import palette
from backend.app.agent_runtime.archive_protocol import commit_archive


def _agents_payload(**overrides):
    payload = {
        "workers": [
            {
                "ticket": "WIKI-146",
                "role": "implement",
                "kind": "cc",
                "state": "working",
                "step": "building palette",
                "orch": "wiki",
                "model": "claude-opus-4-7",
                "spawned_at": "2026-07-22T10:00:00+00:00",
                "run_id": "run-1",
            },
            {
                "ticket": "PHO-14000",
                "role": "implement",
                "kind": "cdx",
                "state": "merge-ready",
                "step": "waiting review",
                "spawned_at": "2026-07-22T09:00:00+00:00",
                "run_id": "run-2",
            },
        ],
        "orchestrators": [
            {
                "id": "wiki",
                "kind": "cc",
                "model": "claude-opus-4-7",
                "cwd": "/Users/henry/me/fun/wiki",
                "spawned_at": "2026-07-22T08:00:00+00:00",
            }
        ],
        "archived": [
            {
                "ticket": "WIKI-99",
                "role": "implement",
                "kind": "cc",
                "outcome": "merged",
                "archived_at": "2026-07-18T12:00:00+00:00",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _make_vault(root: Path) -> Path:
    vault = root / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "todo.md").write_text(
        "---\ntype: reference\n---\n\nTodo:\n\n- [P2] WIKI-146 add command palette\n- [P3] PHO-14099 clean up telemetry\n- [P3] MITMWEB-42 unrelated backlog\n\nIn Progress:\n\n- PHO-14000 waiting review\n",
        encoding="utf-8",
    )
    (vault / "log").mkdir(exist_ok=True)
    (vault / "log" / "done.md").write_text(
        "---\ntype: log\n---\n\n# Done\n\n## 2026-07-21\n\n- **wiki** — WIKI-99 old cleanup PR\n",
        encoding="utf-8",
    )
    (vault / "hot.md").write_text(
        "---\ntype: reference\n---\n\n# Hot Context\n\nRolling cache of active threads.\n",
        encoding="utf-8",
    )
    return vault


def _write_events_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for seq, entry in enumerate(entries, start=1):
        row = {"seq": seq, **entry}
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path.parent / "raw.jsonl").write_text("", encoding="utf-8")
    (path.parent / "run.json").write_text(
        json.dumps({"run_id": path.parent.name, "agent_id": path.parent.parent.name}),
        encoding="utf-8",
    )
    commit_archive(path.parent)


def _artifact_event(kind: str, artifact_id: str, title: str) -> dict:
    payloads = {
        "code": {"language": "python", "source": "print(1)"},
        "mermaid": {"source": "graph TD; A-->B"},
        "diff": {"unified": "--- a\n+++ b\n@@\n-x\n+y\n"},
        "svg": {"source": "<svg/>"},
        "image": {"ref": f"artifact://{artifact_id}", "mime": "image/png", "byte_size": 4},
        "table": {"columns": [{"key": "a", "label": "A", "type": "string"}], "rows": [["hi"]]},
            "plot": {"spec_vega_lite": {"mark": "point"}},
            "json": {"data": {"ok": True}},
            "file-list": {"files": [{"path": "a"}]},
            "video": {"ref": f"artifact://{artifact_id}", "mime": "video/mp4", "byte_size": 4},
            "audio": {"ref": f"artifact://{artifact_id}", "mime": "audio/wav", "byte_size": 4},
            "visual-diff": {
            "before": {
                "ref": f"artifact://{artifact_id}/before",
                "mime": "image/png",
                "byte_size": 4,
                "width": 2,
                "height": 2,
            },
            "after": {
                "ref": f"artifact://{artifact_id}/after",
                "mime": "image/png",
                "byte_size": 4,
                "width": 2,
                "height": 2,
            },
        },
    }
    return {
        "kind": "artifact",
        "artifact_id": artifact_id,
        "payload": {
            "kind": "artifact",
            "id": artifact_id,
            "title": title,
            "caption": f"caption for {title}",
            "artifact": {"kind": kind, **payloads[kind]},
            "ts": "2026-07-22T12:00:00+00:00",
        },
    }


class PaletteSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        palette._note_cache.entries.clear()

    def test_empty_query_returns_recent_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "",
                10,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            self.assertIn(results[0]["kind"], {"session", "ticket", "note", "artifact"})

    def test_query_ranks_session_first_when_ticket_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "WIKI-146",
                10,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0]["kind"], "session")
            self.assertEqual(results[0]["id"], "WIKI-146")
            self.assertEqual(results[0]["url"], "#/agent/WIKI-146")

    def test_note_query_finds_hot_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "hot",
                5,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            notes = [row for row in results if row["kind"] == "note"]
            self.assertTrue(any(row["title"] == "Hot Context" for row in notes))

    def test_ticket_without_session_gets_linear_url_pho(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "PHO-14099",
                5,
                agents_payload=_agents_payload(workers=[], orchestrators=[], archived=[]),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            top = results[0]
            self.assertEqual(top["kind"], "ticket")
            self.assertTrue(top["url"].startswith("https://linear.app/phoebework/issue/PHO-14099"))

    def test_ticket_without_session_gets_linear_url_wiki(self):
        # WIKI-* tickets must also fall back to Linear when no session exists.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            # There is a WIKI-146 line in todo but strip the live worker so no session.
            results = palette.search(
                "WIKI-146",
                5,
                agents_payload=_agents_payload(workers=[], orchestrators=[], archived=[]),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            top = results[0]
            self.assertEqual(top["kind"], "ticket")
            self.assertEqual(top["url"], "https://linear.app/phoebework/issue/WIKI-146")

    def test_ticket_without_session_gets_linear_url_mitmweb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "MITMWEB-42",
                5,
                agents_payload=_agents_payload(workers=[], orchestrators=[], archived=[]),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            top = results[0]
            self.assertEqual(top["kind"], "ticket")
            self.assertEqual(top["url"], "https://linear.app/phoebework/issue/MITMWEB-42")

    def test_limit_capped_and_no_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "",
                -5,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertEqual(len(results), 1)

    def test_scoring_prefers_prefix_over_subsequence(self):
        now = datetime.now(tz=timezone.utc)
        prefix_hit = palette.PaletteItem(
            kind="note",
            id="a",
            title="palette",
            subtitle="",
            url="#",
            updated_at=now,
            haystack="palette overview",
        )
        subseq_hit = palette.PaletteItem(
            kind="note",
            id="b",
            title="playbook alarm launch etc",
            subtitle="",
            url="#",
            updated_at=now,
            haystack="playbook alarm launch etc",
        )
        prefix_score = palette.score_item(prefix_hit, "pal", now)
        subseq_score = palette.score_item(subseq_hit, "pal", now)
        self.assertIsNotNone(prefix_score)
        self.assertIsNotNone(subseq_score)
        self.assertLess(prefix_score, subseq_score)

    def test_relevance_class_beats_recency(self):
        # A stale substring/exact match must still outrank a fresh subsequence.
        now = datetime.now(tz=timezone.utc)
        stale = now - timedelta(days=180)
        exact = palette.PaletteItem(
            kind="note",
            id="a",
            title="palette",
            subtitle="",
            url="#",
            updated_at=stale,
            haystack="palette",
        )
        fresh_subseq = palette.PaletteItem(
            kind="note",
            id="b",
            title="p a l alarm",
            subtitle="",
            url="#",
            updated_at=now,
            haystack="p a l alarm",
        )
        exact_score = palette.score_item(exact, "palette", now)
        fresh_score = palette.score_item(fresh_subseq, "palette", now)
        self.assertIsNotNone(exact_score)
        # fresh may not even match
        if fresh_score is not None:
            self.assertLess(exact_score, fresh_score)

    def test_artifacts_walked_from_archive_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            archive = root / "archive"
            events = archive / "WIKI-99" / "20260721-120000" / "events.jsonl"
            artifact_id = "0f6e3a4c-1234-4c1a-8b5e-abcdefabcdef"
            _write_events_jsonl(
                events,
                [
                    _artifact_event("mermaid", artifact_id, "flow diagram"),
                    _artifact_event("code", "1234abcd-5678-4b90-9c0d-ef0123456789", "handler"),
                    _artifact_event("diff", "2345bcde-6789-4c01-9d1e-f01234567890", "review diff"),
                ],
            )
            results = palette.search(
                "flow",
                10,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=archive,
            )
            artifact_rows = [row for row in results if row["kind"] == "artifact"]
            self.assertTrue(any(row["id"] == artifact_id for row in artifact_rows))
            top = next(row for row in artifact_rows if row["id"] == artifact_id)
            self.assertEqual(top["artifact_id"], artifact_id)
            self.assertEqual(top["ticket"], "WIKI-99")
            url = top["url"]
            self.assertTrue(url.endswith("#/agent/WIKI-99"), url)
            self.assertIn(f"panel=WIKI-99", url)
            self.assertIn(f"artifact={artifact_id}", url)
            self.assertIn(f"focus={artifact_id}", url)
            self.assertIn(f"tab={artifact_id}", url)

    def test_committed_archive_survives_partial_directory_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive"
            ticket_dir = archive / "WIKI-99"
            partial_ids = [
                "20260721-partial",
                "20260720-partial",
                "20260719-partial",
            ]
            for index, session_id in enumerate(partial_ids):
                session_dir = ticket_dir / session_id
                session_dir.mkdir(parents=True)
                (session_dir / "events.jsonl").write_text(
                    json.dumps(
                        _artifact_event(
                            "code", f"partial-{index}", f"partial {index}"
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (session_dir / "raw.jsonl").write_text("", encoding="utf-8")
                (session_dir / "run.json").write_text("{}", encoding="utf-8")

            committed_id = "20260718-committed"
            _write_events_jsonl(
                ticket_dir / committed_id / "events.jsonl",
                [_artifact_event("code", "older-committed", "older committed")],
            )

            items = palette.collect_artifact_items(
                root / "no-runs", archive, max_dirs=1
            )

            self.assertEqual([item.artifact_id for item in items], ["older-committed"])

    def test_artifact_kinds_all_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            archive = root / "archive"
            events = archive / "WIKI-99" / "20260721-120000" / "events.jsonl"
            entries = []
            for kind in (
                "code", "mermaid", "diff", "svg", "table", "plot", "json",
                "video", "audio", "visual-diff",
            ):
                artifact_id = f"aaaaaaaa-{kind[:4]:<4}-4000-8000-000000000000".replace(" ", "0")
                entries.append(_artifact_event(kind, artifact_id, f"{kind} title"))
            _write_events_jsonl(events, entries)
            results = palette.search(
                "title",
                50,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=archive,
            )
            kinds_seen = {row.get("subtitle", "").split(" ")[0] for row in results if row["kind"] == "artifact"}
            for expected in (
                "code", "mermaid", "diff", "svg", "table", "plot", "json",
                "video", "audio", "visual-diff",
            ):
                self.assertIn(expected, kinds_seen, f"missing artifact kind {expected} in {kinds_seen}")

    def test_indexed_archived_artifact_after_old_limit_ranks_by_outer_timestamp(self):
        class IndexedEvents:
            def read_artifact_events(self, **_kwargs):
                old_events = [
                    {
                        "kind": "artifact",
                        "normalized_at": "2026-08-01T00:00:00+00:00",
                        "payload": {
                            "kind": "artifact",
                            "id": f"old-{index}",
                            "title": f"old {index}",
                            "artifact": {"kind": "table"},
                        },
                    }
                    for index in range(801)
                ]
                old_events.append(
                    {
                        "kind": "artifact",
                        "normalized_at": "2026-08-19T00:00:00+00:00",
                        "payload": {
                            "kind": "artifact",
                            "id": "new-archived",
                            "title": "new archived",
                            "artifact": {"kind": "table"},
                        },
                    }
                )
                return iter(("archived", event) for event in old_events)

        items = palette.collect_artifact_items_from_index(
            IndexedEvents(),
            archive_by_run={"archived": ("WIKI-1", "2026-08-19T00:00:00+00:00")},
        )

        self.assertIsNotNone(items)
        self.assertEqual(items[0].artifact_id, "new-archived")
        self.assertEqual(
            items[0].updated_at,
            datetime(2026, 8, 19, tzinfo=timezone.utc),
        )

    def test_symlink_outside_vault_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            outside = root / "outside"
            outside.mkdir()
            secret = outside / "secret.md"
            secret.write_text("# Leak\n\ntop secret content\n", encoding="utf-8")
            link = vault / "leak.md"
            try:
                os.symlink(secret, link)
            except OSError:
                self.skipTest("no symlink support")
            results = palette.search(
                "leak",
                10,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            for row in results:
                self.assertNotEqual(row.get("title"), "Leak")
                self.assertFalse(row.get("url", "").endswith("leak.md"))

    def test_note_cache_avoids_second_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            counter = {"calls": 0}
            real_walk = palette._walk_vault

            def counting_walk(vault_dir: Path, should_cancel=None) -> list:
                counter["calls"] += 1
                return real_walk(vault_dir, should_cancel)

            palette._walk_vault = counting_walk  # type: ignore[assignment]
            try:
                palette._note_cache.entries.clear()
                palette.search("hot", 5, agents_payload=_agents_payload(), vault_dir=vault, runs_dir=root / "no-runs", archive_dir=root / "no-archive")
                palette.search("context", 5, agents_payload=_agents_payload(), vault_dir=vault, runs_dir=root / "no-runs", archive_dir=root / "no-archive")
                self.assertEqual(counter["calls"], 1)
            finally:
                palette._walk_vault = real_walk  # type: ignore[assignment]

    def test_note_cache_invalidates_on_file_edit(self):
        # Editing an existing note updates only its mtime, not the vault dir
        # mtime. A directory-signature-only cache would go stale forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            hot = vault / "hot.md"
            first = palette.search(
                "unique-marker-alpha",
                5,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertFalse(any("unique-marker-alpha" in row.get("subtitle", "") for row in first))

            hot.write_text(
                "---\ntype: reference\n---\n\n# Hot Context\n\nunique-marker-alpha rolling cache of active threads.\n",
                encoding="utf-8",
            )
            # Bump mtime deterministically so the test doesn't race
            # sub-nanosecond filesystem resolution on fast machines.
            future = hot.stat().st_mtime + 5
            os.utime(hot, (future, future))

            second = palette.search(
                "unique-marker-alpha",
                5,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertTrue(
                any("unique-marker-alpha" in row.get("subtitle", "") for row in second),
                f"expected refreshed note in {second}",
            )

    def test_should_cancel_aborts_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            palette._note_cache.entries.clear()
            with self.assertRaises(palette.PaletteCancelled):
                palette.search(
                    "hot",
                    5,
                    agents_payload=_agents_payload(),
                    vault_dir=vault,
                    runs_dir=root / "no-runs",
                    archive_dir=root / "no-archive",
                    should_cancel=lambda: True,
                )

    def test_endpoint_cancels_walk_when_client_disconnects(self):
        # Endpoint-level regression: a client disconnecting mid-walk must set
        # the cancellation event, which the palette walk observes via
        # `should_cancel`, which raises `PaletteCancelled`, which the endpoint
        # converts to HTTP 499. The unit-level `test_should_cancel_aborts_walk`
        # only proves the walk respects the flag when it is already set — this
        # test proves the endpoint actually wires disconnect -> flag -> walk.
        from backend.app import main as app_main

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/palette/search",
            "query_string": b"q=hot",
            "headers": [],
        }

        async def _receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive=_receive)

        # Simulate the client disconnecting on the third disconnect poll,
        # after the walk has definitely started polling `should_cancel`.
        poll_count = {"n": 0}

        async def fake_is_disconnected() -> bool:
            poll_count["n"] += 1
            return poll_count["n"] >= 3

        request.is_disconnected = fake_is_disconnected  # type: ignore[method-assign]

        walk_polls = {"n": 0}
        walk_cancelled = {"raised": False}

        def blocking_search(*args, should_cancel=None, **kwargs):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                walk_polls["n"] += 1
                if should_cancel and should_cancel():
                    walk_cancelled["raised"] = True
                    raise palette.PaletteCancelled()
                time.sleep(0.02)
            raise AssertionError("walk was never cancelled — endpoint plumbing broken")

        async def drive() -> None:
            with mock.patch("backend.app.main.agents", return_value={}), \
                 mock.patch.object(palette, "search", side_effect=blocking_search):
                with self.assertRaises(HTTPException) as caught:
                    await app_main.palette_search(request, q="hot", limit=5)
                self.assertEqual(caught.exception.status_code, 499)

        asyncio.run(drive())

        self.assertGreater(walk_polls["n"], 0, "walk should have started polling should_cancel")
        self.assertTrue(walk_cancelled["raised"], "cancellation should have propagated to the walk")
        self.assertGreaterEqual(poll_count["n"], 3, "disconnect watcher should have polled at least 3 times")

    def test_result_payload_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            results = palette.search(
                "wiki",
                3,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=root / "no-archive",
            )
            self.assertGreater(len(results), 0)
            base = {"kind", "id", "title", "subtitle", "url", "updated_at", "score"}
            allowed = base | {"artifact_id", "ticket"}
            for row in results:
                keys = set(row.keys())
                self.assertTrue(base.issubset(keys), f"missing base keys in {keys}")
                self.assertTrue(keys.issubset(allowed), f"unexpected keys in {keys}")


if __name__ == "__main__":
    unittest.main()
