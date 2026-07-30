from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app import main
from backend.app.agent_runtime import rebase_bot, rebase_durable
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

    def test_import_conflict_escalates_without_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=False
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)
        self.assertEqual(result["resolved_files"], [])

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

    def test_import_additions_escalate_even_with_a_merge_base(self) -> None:
        conflict = (
            "<<<<<<< ours\nimport base\nimport ours\n||||||| base\n"
            "import base\n=======\nimport base\nimport theirs\n>>>>>>> theirs\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            path.write_text(conflict, encoding="utf-8")
            resolved, _detail = rebase_bot.resolve_conflict_file(path)
            content = path.read_text(encoding="utf-8")
        self.assertFalse(resolved)
        self.assertEqual(content, conflict)

    def test_trailing_whitespace_changes_escalate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw), whitespace=True)
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=False
            )
        self.assertEqual(result["status"], "escalated")

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
                    notify=lambda target, text, _id="": messages.append((target, text)),
                )
                self.assertEqual(result["status"], expected_status)

        self.assertEqual([target for target, _message in messages], ["wiki", "wiki"])
        self.assertIn("rebase-bot resolved", messages[0][1])
        self.assertNotIn("escalated", messages[0][1])
        self.assertIn("rebase-bot escalated", messages[1][1])

    def test_injected_test_notifier_records_without_backend_send(self) -> None:
        notifications: list[tuple[str, str]] = []
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "WIKI-175-IMPL",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
            agent_message=mock.Mock(side_effect=AssertionError("live send")),
        )

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
                        "head_sha": "notify-test",
                    }
                },
                helper=lambda _worktree: {
                    "status": "resolved",
                    "head_sha": "notify-test",
                    "resolved_files": [],
                    "escalated_hunks": [],
                },
                notify=lambda target, text, _id="": notifications.append((target, text)),
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(len(notifications), 1)
        fake_main.agent_message.assert_not_called()

    def test_pytest_default_sender_does_not_touch_live_channel(self) -> None:
        with (
            mock.patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "rebase-test"}),
            mock.patch.object(main, "agent_message") as send,
        ):
            main._rebase_bot_notification_sender("wiki", "test notification")
        send.assert_not_called()

    def test_duplicate_logical_notification_is_sent_once(self) -> None:
        sent: list[tuple[str, str]] = []
        result = {
            "status": "resolved",
            "head_sha": "same-sha",
            "resolved_files": [],
            "escalated_hunks": [],
        }
        with mock.patch.object(rebase_bot, "_NOTIFIED_RESULTS", set()):
            for _ in range(5):
                rebase_bot._escalate_to_orchestrator(
                    "WIKI-175-IMPL",
                    "wiki",
                    result,
                    lambda target, text, _id="": sent.append((target, text)),
                    "same-event",
                )
        self.assertEqual(len(sent), 1)

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
        self.assertNotIn("import ordering", source)
        self.assertNotIn("_mechanical_resolution", source)

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

    def test_line_ending_only_resolution_preserves_string_contents(self) -> None:
        conflict = (
            '<<<<<<< ours\r\nvalue = "a b   "\r\n'
            '=======\nvalue = "a b   "\n>>>>>>> theirs\r\n'
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            path.write_bytes(conflict.encode("utf-8"))
            resolved, detail = rebase_bot.resolve_conflict_file(path)
            content = path.read_text(encoding="utf-8")
        self.assertTrue(resolved)
        self.assertIsNone(detail)
        self.assertEqual(content, 'value = "a b   "\n')

    def test_marker_size_bypass_escalates(self) -> None:
        conflict = (
            "<<<<<<<< ours\nvalue = 1\n=======\nvalue = 1\n"
            ">>>>>>> theirs\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            path.write_text(conflict, encoding="utf-8")
            resolved, detail = rebase_bot.resolve_conflict_file(path)
            content = path.read_text(encoding="utf-8")
        self.assertFalse(resolved)
        self.assertIn("malformed conflict marker", detail or "")
        self.assertEqual(content, conflict)

    def test_filename_is_not_interpreted_as_a_lockfile_option(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw)
            with mock.patch.object(rebase_bot.subprocess, "run") as run:
                detail = rebase_bot._regenerate_lockfile(
                    worktree, "--exclude=evil.py"
                )
        self.assertIn("no lockfile generator", detail or "")
        run.assert_not_called()

    def test_pushurl_bypass_escalates_before_push(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            _run(
                worktree,
                "git",
                "remote",
                "set-url",
                "--push",
                "origin",
                "https://attacker.invalid/repo.git",
            )
            self.assertIn(
                "pushurl differs",
                rebase_bot._push_destination_error(worktree) or "",
            )

    def test_parser_exception_restores_original_clean_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            with mock.patch.object(
                rebase_bot,
                "resolve_conflict_file",
                side_effect=RuntimeError("parser crashed"),
            ):
                result = rebase_bot.run_rebase_helper(
                    worktree, smoke_commands=[], push=False
                )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            status = _run(worktree, "git", "status", "--porcelain").stdout
        self.assertEqual(result["status"], "escalated")
        self.assertIn("parser crashed", result["escalated_hunks"][0])
        self.assertEqual(before, after)
        self.assertEqual(status, "")

    def test_nested_lockfile_uses_basename_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw)
            lockfile = worktree / "frontend" / "package-lock.json"
            lockfile.parent.mkdir()

            def creating(cmd, cwd=None, **_kwargs):
                (Path(cwd) / "package-lock.json").write_text(
                    "{}", encoding="utf-8"
                )
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(
                rebase_bot.subprocess, "run", side_effect=creating
            ) as run:
                self.assertIsNone(
                    rebase_bot._regenerate_lockfile(
                        worktree, "frontend/package-lock.json"
                    )
                )
        self.assertEqual(run.call_args.kwargs["cwd"], str(lockfile.parent))
        self.assertEqual(run.call_args.args[0][0], "npm")

    def test_conflicted_lockfile_is_deleted_before_regeneration(self) -> None:
        calls: list[list[str]] = []
        conflict_reads = 0

        def git(_worktree: Path, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal conflict_reads
            calls.append(args)
            if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
                conflict_reads += 1
                output = "frontend/package-lock.json\n" if conflict_reads == 1 else ""
                return subprocess.CompletedProcess([], 0, output, "")
            if args[0] == "merge":
                return subprocess.CompletedProcess([], 1, "", "conflict")
            return subprocess.CompletedProcess([], 0, "sha\n" if args[:2] == ["rev-parse", "HEAD"] else "", "")

        with tempfile.TemporaryDirectory() as raw:
            with (
                mock.patch.object(rebase_bot, "_git", side_effect=git),
                mock.patch.object(rebase_bot, "_regenerate_lockfile", return_value=None) as regenerate,
            ):
                result = rebase_bot.run_rebase_helper(
                    Path(raw), smoke_commands=[], push=False
                )
        self.assertEqual(result["status"], "resolved")
        self.assertIn(["rm", "-f", "--", "frontend/package-lock.json"], calls)
        regenerate.assert_called_once_with(Path(raw).resolve(), "frontend/package-lock.json")

    def test_result_outbox_retries_with_same_delivery_id_until_bound(self) -> None:
        # After R12 the outbox retries a raising sender with the SAME
        # ``delivery_id`` so the receiver (which forwards the id as
        # ``MessageIn.dedupe_key``) can drop duplicates.  Retries stop at
        # ``_OUTBOX_MAX_ATTEMPTS`` so a persistently broken orchestrator
        # cannot hold the outbox forever.
        with tempfile.TemporaryDirectory() as raw:
            seen: list[str] = []

            def failing_sender(_target: str, _message: str, delivery_id: str) -> None:
                seen.append(delivery_id)
                raise RuntimeError("orchestrator unavailable")

            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
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
                "head_sha": "sha-one",
                "resolved_files": [],
                "escalated_hunks": ["semantic"],
            }
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                rebase_durable._DURABLE_STATE_LOADED = False
                rebase_durable._DURABLE_JOBS.clear()
                rebase_durable._OUTBOX.clear()
                rebase_durable._DELIVERED_EVENTS.clear()
                rebase_durable._persist_completion(job, result)

                clock = [time.time()]

                def advancing_clock() -> float:
                    clock[0] += 3600.0
                    return clock[0]

                for _ in range(rebase_durable._OUTBOX_MAX_ATTEMPTS + 3):
                    rebase_bot._flush_outbox(failing_sender, _now=advancing_clock)
        # Bounded number of attempts; every attempt carries the same key.
        self.assertGreater(len(seen), 1)
        self.assertLessEqual(len(seen), rebase_durable._OUTBOX_MAX_ATTEMPTS)
        self.assertTrue(all(sid == seen[0] for sid in seen))
        self.assertEqual(seen[0], "137:sha-one:escalated:sha-one")
        # After the bound the entry is dropped.
        self.assertNotIn("durable-test:result", rebase_durable._OUTBOX)

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
                    175,
                    "WIKI-175",
                    "WIKI-175-IMPL",
                    gate=gate,
                    notify=lambda _target, _text, _id="": None,
                )
                self.assertEqual(first["status"], "started")
                self.assertTrue(started.wait(timeout=2))
                # The retry returns immediately and cannot start another merge.
                second = rebase_bot.rebase_dirty_pr(
                    175,
                    "WIKI-175",
                    "WIKI-175-IMPL",
                    gate=gate,
                    notify=lambda _target, _text, _id="": None,
                )
                release.set()
                self.assertTrue(finished.wait(timeout=2))
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.get("no_op"))

    def test_smoke_failure_escalates_and_restores_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _conflicting_repo(Path(raw))
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            def resolve(path: Path) -> tuple[bool, str | None]:
                path.write_text("import ours\n", encoding="utf-8")
                return True, None

            with mock.patch.object(rebase_bot, "resolve_conflict_file", side_effect=resolve):
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
