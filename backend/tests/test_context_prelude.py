from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app import context_prelude, main


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class ContextPreludeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.status = self.root / "status"
        self.status.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / "shared.py").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "--", "shared.py")
        git(self.root, "commit", "-m", "base")
        git(self.root, "switch", "-c", "old")
        (self.root / "shared.py").write_text("old\n", encoding="utf-8")
        git(self.root, "commit", "-am", "old change")
        git(self.root, "switch", "main")
        git(self.root, "merge", "--no-ff", "old", "-m", "Merge pull request #9 from old/shared")
        git(self.root, "switch", "-c", "feature")
        (self.root / "shared.py").write_text("feature\n", encoding="utf-8")
        git(self.root, "commit", "-am", "feature change")
        (self.vault / "wiki-180.md").write_text(
            "# WIKI-180\n\nThe context prelude uses shared.py and workgraph state.\n",
            encoding="utf-8",
        )
        (self.status / "WIKI-180.workgraph.json").write_text(
            '{"ticket":"WIKI-180","orch":"wiki","nodes":[{"id":"WIKI-180","kind":"implement"}],"edges":[{"kind":"spawn"}]}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def builder(self) -> context_prelude.ContextPreludeBuilder:
        return context_prelude.ContextPreludeBuilder(
            repo_root=self.root,
            vault_dir=self.vault,
            status_dir=self.status,
            runtime_dir=self.runtime,
        )

    def test_builds_from_fixture_sources_and_reports_overlap(self) -> None:
        fake_index = mock.Mock()
        fake_index.search.side_effect = lambda query, **_kwargs: {
            "results": [
                {
                    "path": "wiki-180.md",
                    "snippet": "shared.py and workgraph state",
                }
            ]
        }
        with mock.patch.object(
            context_prelude.knowledge.KnowledgeIndex,
            "from_env",
            return_value=fake_index,
        ):
            result = self.builder().build(
                ticket="WIKI-180",
                title="context prelude",
                prompt="implement the builder",
            )

        self.assertLessEqual(len(result.text), context_prelude.MAX_PRELUDE_CHARS)
        self.assertIn("## vault notes", result.text)
        self.assertIn("## recent merged prs", result.text)
        self.assertIn("shared.py", result.text)
        self.assertIn("## related workgraph", result.text)
        self.assertEqual(result.sources["vault notes"]["status"], "ok")

    def test_each_source_failure_degrades_with_a_note(self) -> None:
        with (
            mock.patch.object(context_prelude, "_vault_source", side_effect=RuntimeError("vault down")),
            mock.patch.object(context_prelude, "_recent_pr_source", side_effect=RuntimeError("git down")),
            mock.patch.object(context_prelude, "_workgraph_source", side_effect=RuntimeError("graph down")),
        ):
            result = self.builder().build(ticket="WIKI-180")

        self.assertIn("vault notes unavailable", result.text)
        self.assertIn("recent merged prs unavailable", result.text)
        self.assertIn("related workgraph unavailable", result.text)
        self.assertEqual(result.sources["recent merged prs"]["status"], "failed")

    def test_budget_is_hard_and_truncation_is_explicit(self) -> None:
        huge = context_prelude.SourceResult("x" * 20_000, "ok")
        with mock.patch.object(context_prelude, "_vault_source", return_value=huge):
            result = self.builder().build(ticket="WIKI-180")

        self.assertLessEqual(len(result.text), context_prelude.MAX_PRELUDE_CHARS)
        self.assertTrue(result.truncated)
        self.assertIn("truncated:", result.text)

    def test_injection_shaped_ticket_is_rejected_before_git(self) -> None:
        with mock.patch.object(context_prelude.subprocess, "run") as run:
            with self.assertRaises(context_prelude.PreludeError):
                self.builder().build(ticket="WIKI-180; touch /tmp/pwned")
        run.assert_not_called()

    def test_prepend_keeps_goal_after_context(self) -> None:
        prompt = context_prelude.prepend("## context\n- note", "run tests")
        self.assertEqual(prompt, "## context\n- note\n\n# kickoff prompt\nrun tests")

    def test_spawn_path_uses_opt_in_prelude_and_fails_soft(self) -> None:
        body = main.SpawnWorkerIn(
            ticket="WIKI-180",
            kind="cc",
            role="implement",
            model="sonnet",
            workdir=str(self.root),
            prompt="run tests",
            context_prelude=True,
        )
        generated = context_prelude.PreludeResult("## generated", False, {})
        with mock.patch.object(main, "_build_context_prelude", return_value=generated):
            self.assertEqual(
                main._contextual_prompt(body, repo_root=self.root),
                "## generated\n\n# kickoff prompt\nrun tests",
            )
        with mock.patch.object(main, "_build_context_prelude", side_effect=RuntimeError("broken")):
            self.assertEqual(main._contextual_prompt(body, repo_root=self.root), "run tests")


if __name__ == "__main__":
    unittest.main()
