from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app import palette


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
            self.assertEqual(top["url"], "#/agent/WIKI-99")

    def test_artifact_kinds_all_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            archive = root / "archive"
            events = archive / "WIKI-99" / "20260721-120000" / "events.jsonl"
            entries = []
            for kind in ("code", "mermaid", "diff", "svg", "table", "plot", "json"):
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
            for expected in ("code", "mermaid", "diff", "svg", "table", "plot", "json"):
                self.assertIn(expected, kinds_seen, f"missing artifact kind {expected} in {kinds_seen}")

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

            def counting_walk(vault_dir: Path) -> list:
                counter["calls"] += 1
                return real_walk(vault_dir)

            palette._walk_vault = counting_walk  # type: ignore[assignment]
            try:
                palette._note_cache.entries.clear()
                palette.search("hot", 5, agents_payload=_agents_payload(), vault_dir=vault, runs_dir=root / "no-runs", archive_dir=root / "no-archive")
                palette.search("context", 5, agents_payload=_agents_payload(), vault_dir=vault, runs_dir=root / "no-runs", archive_dir=root / "no-archive")
                self.assertEqual(counter["calls"], 1)
            finally:
                palette._walk_vault = real_walk  # type: ignore[assignment]

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
