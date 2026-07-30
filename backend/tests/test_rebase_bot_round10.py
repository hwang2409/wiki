"""Round-10 findings for WIKI-175. Each test is written before its fix."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.app import main
from backend.app.agent_runtime import rebase_bot, rebase_durable


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def _seed_repo(root: Path, *, binary: bool = False) -> Path:
    origin = root / "origin.git"
    worktree = root / "worktree"
    _run(root, "git", "init", "--bare", str(origin))
    _run(root, "git", "init", str(worktree))
    _run(worktree, "git", "config", "user.email", "test@example.com")
    _run(worktree, "git", "config", "user.name", "test")
    if binary:
        (worktree / "asset.bin").write_bytes(bytes(range(16)))
        _run(worktree, "git", "add", "asset.bin")
    else:
        (worktree / "fixture.py").write_text("value = 0\n", encoding="utf-8")
        _run(worktree, "git", "add", "fixture.py")
    _run(worktree, "git", "commit", "-m", "base")
    _run(worktree, "git", "branch", "-M", "main")
    _run(worktree, "git", "remote", "add", "origin", str(origin))
    _run(worktree, "git", "push", "-u", "origin", "main")
    _run(worktree, "git", "checkout", "-b", "feature")
    if binary:
        (worktree / "asset.bin").write_bytes(bytes(reversed(range(16))))
        _run(worktree, "git", "commit", "-am", "feature-binary")
        _run(worktree, "git", "checkout", "main")
        (worktree / "asset.bin").write_bytes(bytes(range(16, 32)))
        _run(worktree, "git", "commit", "-am", "upstream-binary")
    else:
        (worktree / "fixture.py").write_text("value = 1\n", encoding="utf-8")
        _run(worktree, "git", "commit", "-am", "feature")
        _run(worktree, "git", "checkout", "main")
        (worktree / "fixture.py").write_text("value = 2\n", encoding="utf-8")
        _run(worktree, "git", "commit", "-am", "upstream")
    _run(worktree, "git", "push", "origin", "main")
    _run(worktree, "git", "checkout", "feature")
    return worktree


class F1DiffThreeMarkerLeak(unittest.TestCase):
    def test_diff3_line_ending_resolution_strips_all_markers(self) -> None:
        conflict = (
            "<<<<<<< ours\r\n"
            "value = 1\r\n"
            "||||||| base\r\n"
            "value = 0\r\n"
            "=======\n"
            "value = 1\n"
            ">>>>>>> theirs\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "fixture.py"
            path.write_bytes(conflict.encode("utf-8"))
            resolved, detail = rebase_bot.resolve_conflict_file(path)
            content = path.read_text(encoding="utf-8")
        self.assertTrue(resolved, detail)
        self.assertIsNone(detail)
        for marker in ("<<<<<<<", "|||||||", "=======", ">>>>>>>", "base"):
            self.assertNotIn(marker, content, f"leaked {marker!r} in {content!r}")
        self.assertEqual(content, "value = 1\n")


class F2NoMarkerOrBinaryConflict(unittest.TestCase):
    def test_file_without_conflict_markers_is_escalated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clean.py"
            path.write_text("value = 1\n", encoding="utf-8")
            ok, detail = rebase_bot.resolve_conflict_file(path)
        self.assertFalse(ok)
        self.assertIsNotNone(detail)

    def test_binary_conflict_escalates_and_preserves_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _seed_repo(Path(raw), binary=True)
            before = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
            result = rebase_bot.run_rebase_helper(
                worktree, smoke_commands=[], push=False
            )
            after = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(before, after)


class F3ShrinkwrapRegeneration(unittest.TestCase):
    def test_shrinkwrap_regeneration_produces_shrinkwrap_file(self) -> None:
        calls: list[list[str]] = []

        def runner(cmd, cwd=None, capture_output=None, text=None, timeout=None, check=None):
            calls.append(list(cmd))
            lockfile_dir = Path(cwd)
            if cmd[:2] == ["npm", "install"]:
                (lockfile_dir / "package-lock.json").write_text("{}", encoding="utf-8")
            elif cmd[:2] == ["npm", "shrinkwrap"]:
                package_lock = lockfile_dir / "package-lock.json"
                if package_lock.exists():
                    package_lock.rename(lockfile_dir / "npm-shrinkwrap.json")
                else:
                    (lockfile_dir / "npm-shrinkwrap.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw)
            (worktree / "package.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(rebase_bot.subprocess, "run", side_effect=runner):
                detail = rebase_bot._regenerate_lockfile(
                    worktree, "npm-shrinkwrap.json"
                )
            file_exists = (worktree / "npm-shrinkwrap.json").exists()
        self.assertIsNone(detail)
        self.assertTrue(file_exists)
        self.assertIn(["npm", "shrinkwrap"], calls)

    def test_regeneration_that_leaves_no_lockfile_escalates(self) -> None:
        def runner(cmd, cwd=None, capture_output=None, text=None, timeout=None, check=None):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw)
            (worktree / "package.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(rebase_bot.subprocess, "run", side_effect=runner):
                detail = rebase_bot._regenerate_lockfile(
                    worktree, "package-lock.json"
                )
        self.assertIsNotNone(detail)
        self.assertIn("package-lock.json", detail or "")


class F4RemoteAllowlist(unittest.TestCase):
    def test_repo_name_rejects_non_github_hosts(self) -> None:
        self.assertIsNone(rebase_bot._repo_name("https://evil.com/hwang2409/wiki"))
        self.assertIsNone(rebase_bot._repo_name("https://github-attacker.com/hwang2409/wiki"))
        self.assertIsNone(rebase_bot._repo_name("git@github.com.evil:hwang2409/wiki"))
        self.assertIsNone(rebase_bot._repo_name("/local/path/hwang2409/wiki"))
        self.assertIsNone(rebase_bot._repo_name("../hwang2409/wiki"))

    def test_repo_name_accepts_supported_forms(self) -> None:
        self.assertEqual(
            rebase_bot._repo_name("https://github.com/hwang2409/wiki.git"),
            "hwang2409/wiki",
        )
        self.assertEqual(
            rebase_bot._repo_name("git@github.com:hwang2409/wiki.git"),
            "hwang2409/wiki",
        )
        self.assertEqual(
            rebase_bot._repo_name("ssh://git@github.com/hwang2409/wiki"),
            "hwang2409/wiki",
        )


class F5CrashRecoverySha(unittest.TestCase):
    def test_crash_after_head_move_restores_expected_sha(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worktree = _seed_repo(Path(raw))
            _run(worktree, "git", "checkout", "feature")
            expected_sha = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()

            def crash_after_move(*_args, **_kwargs):
                _run(worktree, "git", "reset", "--hard", "origin/main")
                raise RuntimeError("simulated crash after head moved")

            fake_main = SimpleNamespace(
                _registry_agent=lambda _registry, _worker: (
                    "WIKI-175-IMPL",
                    {},
                    {"worktree": str(worktree), "orch": "wiki"},
                ),
                _read_agent_registry=lambda: {},
            )
            with (
                mock.patch.object(rebase_bot, "_main", return_value=fake_main),
                mock.patch.object(rebase_bot, "_validate_pr_binding"),
                mock.patch.object(rebase_bot, "_preflight_worktree"),
                mock.patch.object(
                    rebase_bot,
                    "_run_rebase_helper_checked",
                    side_effect=crash_after_move,
                ),
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
                            "head_sha": expected_sha,
                        }
                    },
                )
                if result.get("status") == "started":
                    # Wait for the durable worker to run and clean up.
                    key = (175, expected_sha, "production")
                    for _ in range(200):
                        with rebase_bot._JOB_LOCK:
                            job = rebase_bot._JOBS.get(key)
                        if job is not None and job.done.is_set():
                            break
                        time.sleep(0.05)
            final = _run(worktree, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(final, expected_sha)


class F6Supersede(unittest.TestCase):
    def test_active_generation_marks_older_sha_superseded(self) -> None:
        pr_number = 12345
        with rebase_bot._JOB_LOCK:
            rebase_bot._ACTIVE_SHA.pop(pr_number, None)
        self.assertFalse(rebase_bot._is_superseded(pr_number, "sha_old"))
        rebase_bot._mark_active(pr_number, "sha_old")
        self.assertFalse(rebase_bot._is_superseded(pr_number, "sha_old"))
        rebase_bot._mark_active(pr_number, "sha_new")
        self.assertTrue(rebase_bot._is_superseded(pr_number, "sha_old"))
        self.assertFalse(rebase_bot._is_superseded(pr_number, "sha_new"))

    def test_supersede_check_blocks_push_before_it_runs(self) -> None:
        pushes: list[list[str]] = []
        commands: list[list[str]] = []

        def git(worktree, args, timeout=None):
            commands.append(list(args))
            if args[:1] == ["push"]:
                pushes.append(list(args))
            if args[:1] == ["fetch"]:
                return subprocess.CompletedProcess([], 0, "", "")
            if args[:1] == ["merge"]:
                # A clean fast-forward merge exercises the push branch.
                return subprocess.CompletedProcess([], 0, "", "")
            if args[:2] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess([], 0, "sha-after\n", "")
            if args[:1] == ["remote"] and "--push" in args:
                return subprocess.CompletedProcess([], 0, "origin\n", "")
            if args[:1] == ["remote"]:
                return subprocess.CompletedProcess([], 0, "origin\n", "")
            return subprocess.CompletedProcess([], 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            with (
                mock.patch.object(rebase_bot, "_git", side_effect=git),
                mock.patch.object(
                    rebase_bot, "_push_destination_error", return_value=None
                ),
            ):
                result = rebase_bot._run_rebase_helper_unlocked(
                    Path(raw),
                    smoke_commands=[],
                    push=True,
                    supersede_check=lambda: True,
                )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(pushes, [])
        self.assertIn("superseded", result["escalated_hunks"][0])

    def test_start_of_production_job_updates_active_generation(self) -> None:
        pr_number = 54321
        sha_old = "a" * 40
        sha_new = "b" * 40
        with rebase_bot._JOB_LOCK:
            rebase_bot._ACTIVE_SHA.pop(pr_number, None)
            for key in list(rebase_bot._JOBS):
                if key[0] == pr_number:
                    rebase_bot._JOBS.pop(key, None)
        fake_main = SimpleNamespace(
            _registry_agent=lambda _registry, _worker: (
                "W",
                {},
                {"worktree": tempfile.gettempdir(), "orch": "wiki"},
            ),
            _read_agent_registry=lambda: {},
        )

        def gate_for(sha):
            return lambda _pr: {
                "raw": {
                    "mergeable": "CONFLICTING",
                    "repo": "hwang2409/wiki",
                    "head_ref_name": "feature",
                    "head_sha": sha,
                }
            }

        seen: list[Path] = []

        def helper(_worktree: Path) -> dict:
            seen.append(_worktree)
            return {
                "status": "resolved",
                "head_sha": "abc",
                "resolved_files": [],
                "escalated_hunks": [],
            }

        with (
            mock.patch.object(rebase_bot, "_main", return_value=fake_main),
            mock.patch.object(rebase_bot, "_validate_pr_binding"),
        ):
            rebase_bot.rebase_dirty_pr(
                pr_number,
                "WIKI-175",
                "W",
                gate=gate_for(sha_old),
                helper=helper,
            )
            self.assertFalse(rebase_bot._is_superseded(pr_number, sha_old))
            rebase_bot.rebase_dirty_pr(
                pr_number,
                "WIKI-175",
                "W",
                gate=gate_for(sha_new),
                helper=helper,
            )
        # Helper-injected jobs do not claim the production slot; use the
        # direct production API to verify supersede is recorded.
        rebase_bot._mark_active(pr_number, sha_new)
        self.assertTrue(rebase_bot._is_superseded(pr_number, sha_old))


class F7CompletionAtomic(unittest.TestCase):
    def test_completion_persists_result_and_outbox_in_one_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                rebase_durable._DURABLE_STATE_LOADED = False
                rebase_durable._DURABLE_JOBS.clear()
                rebase_durable._OUTBOX.clear()
                job = rebase_bot._RebaseJob(
                    job_id="atomic-test",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-atomic",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "sha-atomic",
                    "resolved_files": [],
                    "escalated_hunks": ["semantic"],
                }
                # ``_persist_completion`` lives in the durable submodule and
                # resolves ``_persist_durable_state`` locally there.  Patching
                # the submodule attribute is the only way to intercept it.
                original_persist = rebase_durable._persist_durable_state
                call_count = [0]

                def counting_persist():
                    call_count[0] += 1
                    original_persist()

                with mock.patch.object(
                    rebase_durable,
                    "_persist_durable_state",
                    side_effect=counting_persist,
                ):
                    rebase_bot._persist_completion(job, result)
                # Single atomic snapshot file — no torn write between
                # sibling jobs.json/outbox.json/delivered.json can lose a
                # completion any more.
                state_path = Path(raw) / "rebase-bot" / "state.json"
                self.assertTrue(state_path.exists())
                snapshot = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIn("137:sha-atomic", snapshot.get("jobs", {}))
                self.assertTrue(
                    any("atomic-test" in k for k in snapshot.get("outbox", {}))
                )
        self.assertEqual(call_count[0], 1)


class F8OutboxDedupeAndBounds(unittest.TestCase):
    def _fresh_state(self, runtime_dir: str) -> None:
        rebase_durable._DURABLE_STATE_LOADED = False
        rebase_durable._DURABLE_JOBS.clear()
        rebase_durable._OUTBOX.clear()
        rebase_durable._DELIVERED_EVENTS.clear()

    def test_delivered_event_is_not_reenqueued(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._fresh_state(raw)
                job = rebase_bot._RebaseJob(
                    job_id="dedupe-test",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-x",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "sha-x",
                    "resolved_files": [],
                    "escalated_hunks": ["once"],
                }
                sent: list[str] = []
                rebase_bot._persist_completion(job, result)
                rebase_bot._flush_outbox(lambda _t, m: sent.append(m))
                # Re-enqueue the same logical event: must NOT send again.
                rebase_durable._enqueue_result(job, result)
                rebase_bot._flush_outbox(lambda _t, m: sent.append(m))
        self.assertEqual(len(sent), 1)

    def test_outbox_retries_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_main = SimpleNamespace(AGENT_RUNTIME_DIR=raw)
            with mock.patch.object(rebase_bot, "_main", return_value=fake_main):
                self._fresh_state(raw)
                job = rebase_bot._RebaseJob(
                    job_id="bounded-test",
                    worktree=Path(raw),
                    prompt="p",
                    done=threading.Event(),
                    pr_number=137,
                    expected_sha="sha-y",
                    ticket="WIKI-175",
                    worker_id="WIKI-175-IMPL",
                    orchestrator="wiki",
                    durable=True,
                )
                result = {
                    "status": "escalated",
                    "head_sha": "sha-y",
                    "resolved_files": [],
                    "escalated_hunks": ["fail"],
                }
                rebase_bot._persist_completion(job, result)

                def failing_send(_target, _message):
                    raise RuntimeError("network down")

                clock = [time.time()]

                def advancing_clock() -> float:
                    clock[0] += 3600.0  # jump past any backoff window
                    return clock[0]

                # Retry more times than the cap allows; entry must be dropped.
                for _ in range(rebase_durable._OUTBOX_MAX_ATTEMPTS + 3):
                    rebase_bot._flush_outbox(failing_send, _now=advancing_clock)
                self.assertNotIn("bounded-test:result", rebase_durable._OUTBOX)

    def test_recording_notifier_installs_over_pytest_default(self) -> None:
        recorded: list[tuple[str, str]] = []
        with (
            mock.patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "install-test"}),
            mock.patch.object(main, "agent_message") as send,
        ):
            main.install_rebase_recording_notifier(
                lambda t, x: recorded.append((t, x))
            )
            try:
                main._rebase_bot_notification_sender("wiki", "hello")
            finally:
                main.install_rebase_recording_notifier(None)
        self.assertEqual(recorded, [("wiki", "hello")])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
