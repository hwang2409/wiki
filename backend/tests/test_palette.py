from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
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
        "---\ntype: reference\n---\n\nTodo:\n\n- [P2] WIKI-146 add command palette\n- [P3] PHO-14099 clean up telemetry\n\nIn Progress:\n\n- PHO-14000 waiting review\n",
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


class PaletteSearchTests(unittest.TestCase):
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
            # First result must be one of the recent kinds.
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

    def test_ticket_without_session_gets_linear_url(self):
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

    def test_artifacts_walked_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            archive = root / "archive"
            (archive / "WIKI-99" / "20260721-120000" / "artifacts").mkdir(parents=True)
            artifact_id = "0f6e3a4c-1234-4c1a-8b5e-abcdefabcdef"
            (archive / "WIKI-99" / "20260721-120000" / "artifacts" / f"{artifact_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            results = palette.search(
                artifact_id[:8],
                10,
                agents_payload=_agents_payload(),
                vault_dir=vault,
                runs_dir=root / "no-runs",
                archive_dir=archive,
            )
            self.assertTrue(any(row["kind"] == "artifact" for row in results))

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
            required = {"kind", "id", "title", "subtitle", "url", "updated_at", "score"}
            for row in results:
                self.assertEqual(set(row.keys()), required)


if __name__ == "__main__":
    unittest.main()
