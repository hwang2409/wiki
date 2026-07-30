from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
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
        ours = "import base\nimport ours\n"
        theirs = "import base\nimport theirs\n"
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

    def test_import_replacements_and_reorders_escalate(self) -> None:
        replacement = (
            "<<<<<<< ours\nimport ours\n||||||| base\nimport base\n=======\n"
            "import theirs\n>>>>>>> theirs\n"
        )
        reordered = (
            "<<<<<<< ours\nimport b\nimport a\n||||||| base\nimport a\n"
            "import b\n=======\nimport a\nimport b\n>>>>>>> theirs\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            replacement_path = Path(raw) / "replacement.py"
            reordered_path = Path(raw) / "reordered.py"
            replacement_path.write_text(replacement, encoding="utf-8")
            reordered_path.write_text(reordered, encoding="utf-8")
            self.assertFalse(rebase_bot.resolve_conflict_file(replacement_path)[0])
            self.assertFalse(rebase_bot.resolve_conflict_file(reordered_path)[0])

    def test_import_additions_use_the_merge_base(self) -> None:
        conflict = (
            "<<<<<<< ours\nimport base\nimport ours\n||||||| base\n"
            "import base\n=======\nimport base\nimport theirs\n>>>>>>> theirs\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            path.write_text(conflict, encoding="utf-8")
            resolved, _detail = rebase_bot.resolve_conflict_file(path)
            content = path.read_text(encoding="utf-8")
        self.assertTrue(resolved)
        self.assertEqual(content, "import base\nimport ours\nimport theirs\n")

    def test_whitespace_changes_escalate_without_formatter(self) -> None:
        formatter_calls: list[tuple[Path, list[str]]] = []

        def formatter(worktree: Path, files: list[str]) -> None:
            formatter_calls.append((worktree, list(files)))

        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), whitespace=True)
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], formatter=formatter, push=False
            )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(formatter_calls, [])

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
        self.assertIn("fixture.py", result["escalated_hunks"][0])

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

    def test_binding_preserves_branch_names_with_slashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            _run(worktree, "git", "branch", "-m", "codex/foo")
            _run(worktree, "git", "push", "-u", "origin", "codex/foo")
            _run(
                worktree,
                "git",
                "remote",
                "set-url",
                "origin",
                "https://github.com/hwang2409/wiki.git",
            )
            head_sha = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            rebase_bot._validate_pr_binding(
                worktree,
                {
                    "raw": {
                        "repo": "hwang2409/wiki",
                        "head_ref_name": "codex/foo",
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

    def test_assignment_additions_with_same_name_are_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            original = (
                "<<<<<<< ours\nMODE = 'ours'\n||||||| base\n=======\n"
                "MODE = 'theirs'\n>>>>>>> theirs\n"
            )
            path.write_text(original, encoding="utf-8")
            resolved, detail = rebase_bot.resolve_conflict_file(path)
            self.assertFalse(resolved)
            self.assertIn("MODE = 'ours'", detail or "")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_whitespace_comparison_keeps_string_contents(self) -> None:
        self.assertTrue(
            rebase_bot._is_whitespace_only(['value = "a b"\n'], ['value = "a b"\r\n'])
        )
        self.assertFalse(
            rebase_bot._is_whitespace_only(['value = "a b"\n'], ['value = "ab"\n'])
        )
        self.assertFalse(
            rebase_bot._is_whitespace_only(
                ['value = "text   "\n'], ['value = "text"\n']
            )
        )

    def test_formatter_exception_restores_original_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()

            def formatter(_worktree: Path, _files: list[str]) -> None:
                raise RuntimeError("formatter crashed")

            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], formatter=formatter, push=False
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            status = _run(worktree, "git", "status", "--porcelain").stdout
        self.assertEqual(result["status"], "escalated")
        self.assertIn("formatter crashed", result["escalated_hunks"][0])
        self.assertEqual(before, after)
        self.assertEqual(status, "")

    def test_nested_lockfile_uses_basename_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw)
            lockfile = worktree / "frontend" / "package-lock.json"
            lockfile.parent.mkdir()
            with mock.patch.object(
                rebase_bot.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run:
                self.assertIsNone(
                    rebase_bot._regenerate_lockfile(
                        worktree, "frontend/package-lock.json"
                    )
                )
        self.assertEqual(run.call_args.kwargs["cwd"], str(lockfile.parent))
        self.assertEqual(run.call_args.args[0][0], "npm")

    def test_result_outbox_persists_failed_delivery_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            delivered: list[str] = []
            should_fail = True

            def send(_target: str, message: object, _tasks: object) -> None:
                if should_fail:
                    raise RuntimeError("orchestrator unavailable")
                delivered.append(message.text)

            fake_main = SimpleNamespace(
                AGENT_RUNTIME_DIR=raw,
                MessageIn=lambda **kwargs: SimpleNamespace(**kwargs),
                BackgroundTasks=lambda: object(),
                agent_message=send,
            )
            job = rebase_bot._RebaseJob(
                job_id="durable-test",
                worktree=Path(raw),
                prompt="prompt",
                done=threading.Event(),
                pr_number=137,
                expected_sha="sha-one",
                ticket="WIKI-175",
                worker_id="WIKI-175-IMPL",
                orchestrator="wiki",
                durable=True,
            )
            result = {
                "status": "escalated",
                "resolved_files": [],
                "escalated_hunks": ["semantic"],
            }
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                rebase_bot._DURABLE_STATE_LOADED = False
                rebase_bot._DURABLE_JOBS.clear()
                rebase_bot._OUTBOX.clear()
                rebase_bot._persist_job(job, result)
                rebase_bot._enqueue_result(job, result)
                rebase_bot._flush_outbox()
                pending = json.loads(
                    (Path(raw) / "rebase-bot" / "outbox.json").read_text(
                        encoding="utf-8"
                    )
                )
                should_fail = False
                rebase_bot._flush_outbox()
        self.assertEqual(pending["durable-test:result"]["attempts"], 1)
        self.assertEqual(delivered, ["rebase-bot escalated WIKI-175-IMPL: semantic"])

    def test_preflight_rejects_unstaged_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            (worktree / "fixture.py").write_text("local edit\n", encoding="utf-8")
            with self.assertRaisesRegex(RebaseError, "not clean"):
                rebase_bot._preflight_worktree(worktree)

    def test_retry_joins_running_job_without_second_merge(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls: list[Path] = []
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
        )

        def slow_run(worktree: Path, **_kwargs: object) -> dict:
            calls.append(worktree)
            started.set()
            release.wait(timeout=5)
            finished.set()
            return {
                "status": "resolved",
                "head_sha": "abc",
                "resolved_files": [],
                "escalated_hunks": [],
            }

        with (
            tempfile.TemporaryDirectory() as raw,
            mock.patch.object(rebase_bot, "_main", return_value=fake_main),
            mock.patch.object(rebase_bot, "_validate_pr_binding"),
            mock.patch.object(rebase_bot, "_preflight_worktree"),
        ):
            fake_main._registry_agent = lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": raw, "orch": "wiki"},
            )
            expected_sha = f"retry-test-{id(raw)}"

            def gate(_pr: int) -> dict:
                return {
                    "raw": {
                        "mergeable": "CONFLICTING",
                        "repo": "hwang2409/wiki",
                        "head_ref_name": "feature",
                        "head_sha": expected_sha,
                    }
                }

            with mock.patch.object(
                rebase_bot, "_run_rebase_helper_checked", side_effect=slow_run
            ):
                first = rebase_bot.rebase_dirty_pr(
                    175, "WIKI-175", "WIKI-175-IMPL", gate=gate
                )
                self.assertEqual(first["status"], "started")
                self.assertTrue(started.wait(timeout=2))
                # The retry returns immediately and cannot start another merge.
                second = rebase_bot.rebase_dirty_pr(
                    175, "WIKI-175", "WIKI-175-IMPL", gate=gate
                )
                release.set()
                self.assertTrue(finished.wait(timeout=2))
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.get("no_op"))

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
