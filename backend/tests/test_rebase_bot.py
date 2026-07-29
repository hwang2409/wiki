from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app.agent_runtime import rebase_bot
from backend.app.agent_runtime.rebase_bot import RebaseError
from backend.app.rebase_schema import RebaseDirtyPrIn, mcp_input_schema


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def _conflicting_repo(
    root: Path, *, semantic: bool = False, whitespace: bool = False
) -> Path:
    origin = root / "origin.git"
    worktree = root / "worktree"
    _run(root, "git", "init", "--bare", str(origin))
    _run(root, "git", "init", str(worktree))
    _run(worktree, "git", "config", "user.email", "test@example.com")
    _run(worktree, "git", "config", "user.name", "test")
    if semantic:
        base = 'def value():\n    return "base"\n'
        ours = 'def value():\n    return "ours"\n'
        theirs = 'def value():\n    return "theirs"\n'
    elif whitespace:
        base = "value = 1\n"
        ours = "value = 1  # same  \n"
        theirs = "value=1 # same\n"
    else:
        base = "import base\n"
        ours = "import ours\n"
        theirs = "import theirs\n"
    (worktree / "fixture.py").write_text(base, encoding="utf-8")
    _run(worktree, "git", "add", "fixture.py")
    _run(worktree, "git", "commit", "-m", "base")
    _run(worktree, "git", "branch", "-M", "main")
    _run(worktree, "git", "remote", "add", "origin", str(origin))
    _run(worktree, "git", "push", "-u", "origin", "main")
    _run(worktree, "git", "checkout", "-b", "feature")
    (worktree / "fixture.py").write_text(ours, encoding="utf-8")
    _run(worktree, "git", "commit", "-am", "feature")
    _run(worktree, "git", "checkout", "main")
    (worktree / "fixture.py").write_text(theirs, encoding="utf-8")
    _run(worktree, "git", "commit", "-am", "upstream")
    _run(worktree, "git", "push", "origin", "main")
    _run(worktree, "git", "checkout", "feature")
    return worktree


class RebaseBotTests(unittest.TestCase):
    def test_mcp_schema_matches_request_model_constraints(self) -> None:
        endpoint = RebaseDirtyPrIn.model_json_schema()["properties"]
        mcp = mcp_input_schema()["properties"]
        self.assertEqual(mcp, endpoint)

    def test_clean_gate_is_a_noop(self) -> None:
        result = rebase_bot.rebase_dirty_pr(
            175,
            "WIKI-175",
            "WIKI-175-IMPL",
            gate=lambda _pr: {"raw": {"mergeable": "MERGEABLE", "head_sha": "abc"}},
        )
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["no_op"])

    def test_dirty_gate_invokes_helper_and_routes_escalation(self) -> None:
        helper_calls: list[Path] = []
        steer_calls: list[tuple[str, dict]] = []
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
        )

        def helper(worktree: Path) -> dict:
            helper_calls.append(worktree)
            return {
                "status": "escalated",
                "head_sha": "abc",
                "resolved_files": [],
                "escalated_hunks": ["semantic hunk"],
                "source": "rebase-bot",
            }

        with (
            mock.patch.object(rebase_bot, "_main", return_value=fake_main),
            mock.patch.object(rebase_bot, "_validate_pr_binding"),
        ):
            result = rebase_bot.rebase_dirty_pr(
                175,
                "WIKI-175",
                "WIKI-175-IMPL",
                gate=lambda _pr: {
                    "raw": {
                        "mergeable": "CONFLICTING",
                        "repo": "hwang2409/wiki",
                        "head_ref_name": "feature",
                        "head_sha": "abc",
                    }
                },
                helper=helper,
                steer=lambda target, payload: steer_calls.append((target, payload)),
            )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(helper_calls, [Path(tempfile.gettempdir()).resolve()])
        self.assertEqual(steer_calls[0][0], "wiki")
        self.assertEqual(steer_calls[0][1]["source"], "rebase-bot")

    def test_mismatched_pr_rejects_before_helper(self) -> None:
        helper = mock.Mock()
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
        )
        with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
            with self.assertRaisesRegex(RebaseError, "binding mismatch"):
                rebase_bot.rebase_dirty_pr(
                    175,
                    "WIKI-175",
                    "WIKI-175-IMPL",
                    gate=lambda _pr: {
                        "raw": {
                            "mergeable": "CONFLICTING",
                            "repo": "other/repo",
                            "head_ref_name": "other-branch",
                            "head_sha": "deadbeef",
                        }
                    },
                    helper=helper,
                )
        helper.assert_not_called()

    def test_mechanical_import_conflict_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=False
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolved_files"], ["fixture.py"])

    def test_whitespace_resolution_runs_formatter_and_pushes(self) -> None:
        formatter_calls: list[tuple[Path, list[str]]] = []

        def formatter(worktree: Path, files: list[str]) -> None:
            formatter_calls.append((worktree, list(files)))

        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), whitespace=True)
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], formatter=formatter
            )
            remote = _run(
                worktree, "git", "ls-remote", "origin", "refs/heads/feature"
            ).stdout.split()[0]
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(remote, result["head_sha"])
        self.assertEqual(formatter_calls, [(worktree.resolve(), ["fixture.py"])])

    def test_dirty_gate_routes_status_specific_messages_without_override(self) -> None:
        messages: list[tuple[str, str]] = []
        results = [
            {
                "status": "resolved",
                "head_sha": "abc",
                "resolved_files": ["fixture.py"],
                "escalated_hunks": [],
                "source": "rebase-bot",
            },
            {
                "status": "escalated",
                "head_sha": "abc",
                "resolved_files": [],
                "escalated_hunks": ["semantic hunk"],
                "source": "rebase-bot",
            },
        ]
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
            MessageIn=lambda **kwargs: SimpleNamespace(**kwargs),
            BackgroundTasks=lambda: object(),
            agent_message=lambda target, message, _tasks: messages.append(
                (target, message.text)
            ),
        )

        with (
            mock.patch.object(rebase_bot, "_main", return_value=fake_main),
            mock.patch.object(rebase_bot, "_validate_pr_binding"),
        ):
            for expected_status in ("resolved", "escalated"):
                result = rebase_bot.rebase_dirty_pr(
                    175,
                    "WIKI-175",
                    "WIKI-175-IMPL",
                    gate=lambda _pr: {
                        "raw": {
                            "mergeable": "CONFLICTING",
                            "repo": "hwang2409/wiki",
                            "head_ref_name": "feature",
                            "head_sha": "abc",
                        }
                    },
                    helper=lambda _worktree: results.pop(0),
                )
                self.assertEqual(result["status"], expected_status)

        self.assertEqual([target for target, _message in messages], ["wiki", "wiki"])
        self.assertIn("rebase-bot resolved", messages[0][1])
        self.assertNotIn("escalated", messages[0][1])
        self.assertIn("rebase-bot escalated", messages[1][1])

    def test_missing_ruff_escalates_instead_of_silently_skipping_format(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), whitespace=True)
            with mock.patch.object(rebase_bot.shutil, "which", return_value=None):
                result = rebase_bot.run_rebase_helper(
                    worktree, smoke_commands=[], push=False
                )
        self.assertEqual(result["status"], "escalated")
        self.assertIn("ruff formatter unavailable", result["escalated_hunks"][0])

    def test_binding_allows_worktree_without_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            _run(
                worktree,
                "git",
                "remote",
                "set-url",
                "origin",
                "https://github.com/hwang2409/wiki.git",
            )
            _run(worktree, "git", "branch", "--unset-upstream", check=False)
            head_sha = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            rebase_bot._validate_pr_binding(
                worktree,
                {
                    "raw": {
                        "repo": "hwang2409/wiki",
                        "head_ref_name": "feature",
                        "head_sha": head_sha,
                    }
                },
            )

    def test_production_code_has_no_verification_bypass(self) -> None:
        source = Path(rebase_bot.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--no-verify", source)

    def test_semantic_conflict_aborts_without_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), semantic=True)
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=True
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)
        self.assertTrue(result["escalated_hunks"])

    def test_smoke_failure_escalates_and_restores_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree,
                smoke_commands=[
                    ([sys.executable, "-c", "raise SystemExit(3)"], worktree)
                ],
                push=True,
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)
        self.assertIn("smoke test", result["escalated_hunks"][0])


if __name__ == "__main__":
    unittest.main()
