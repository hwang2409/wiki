"""Unit + integration tests for the codex account watchdog + rotation.

Every path referenced here is a temp dir. Never touches real credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import accounts


REAL_LIMIT_STRING = (
    "■ You've hit your usage li" "mit. Visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at Jul 9th, 2026 8:36 PM."
)
BENIGN_LIMIT_STRING = (
    "usage li" "mit resets available. Run /usage for details."
)
CLAUDE_LIMIT_STRING = "Claude usage li" "mit reached. Try again at 4pm."


class _EnvOverride:
    """Isolate accounts module I/O paths to a temp dir."""

    def __init__(self) -> None:
        self._tmp: TemporaryDirectory[str] | None = None
        self._patches: list = []

    def __enter__(self) -> dict[str, Path]:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        paths = {
            "root": root,
            "auth": root / "codex" / "auth.json",
            "accounts": root / "codex-accounts",
            "sessions": root / "codex" / "sessions",
            "rotation_log": root / "codex-accounts" / "rotation.log",
            "registry": root / "agent-registry.json",
            "wiki_cli": root / "fake-wiki",
        }
        env = {
            "WIKI_CODEX_AUTH_PATH": str(paths["auth"]),
            "WIKI_CODEX_ACCOUNTS_DIR": str(paths["accounts"]),
            "WIKI_CODEX_SESSIONS_DIR": str(paths["sessions"]),
            "WIKI_ROTATION_LOG_PATH": str(paths["rotation_log"]),
            "WIKI_AGENT_REGISTRY_PATH": str(paths["registry"]),
            "WIKI_CLI_PATH": str(paths["wiki_cli"]),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self._patches.append(patcher)
        paths["accounts"].mkdir(parents=True, exist_ok=True)
        paths["auth"].parent.mkdir(parents=True, exist_ok=True)
        return paths

    def __exit__(self, exc_type, exc, tb) -> None:
        for patcher in self._patches:
            patcher.stop()
        if self._tmp:
            self._tmp.cleanup()


def _ignore_pipe_pane(_window: str, _log_path: str) -> None:
    return None


def _wiki_agent_update_ok(
    _ticket: str,
    _window: str,
    _log_path: str,
    sid: str | None = None,
) -> tuple[bool, str | None]:
    del sid
    return True, None


class DetectionTests(unittest.TestCase):
    def test_matches_the_real_codex_limit_string(self) -> None:
        self.assertTrue(accounts.detect_codex_limit(REAL_LIMIT_STRING))

    def test_matches_curly_apostrophe_variant(self) -> None:
        variant = REAL_LIMIT_STRING.replace("You've", "You’ve")
        self.assertTrue(accounts.detect_codex_limit(variant))

    def test_matches_have_variant(self) -> None:
        variant = "■ You have hit your usage li" "mit."
        self.assertTrue(accounts.detect_codex_limit(variant))

    def test_ignores_limit_text_inside_quoted_test_output(self) -> None:
        fixture = "■ You've hit your usage li" "mit; try again at 8:01 PM."
        pane = f'fixture = "{fixture}"\nE AssertionError: {fixture!r}'
        self.assertFalse(accounts.detect_codex_limit(pane))

    def test_ignores_unadorned_limit_text(self) -> None:
        pane = "You've hit your usage li" "mit; try again at 8:01 PM."
        self.assertFalse(accounts.detect_codex_limit(pane))

    def test_ignores_benign_usage_reset_line(self) -> None:
        self.assertFalse(accounts.detect_codex_limit(BENIGN_LIMIT_STRING))

    def test_ignores_empty_pane(self) -> None:
        self.assertFalse(accounts.detect_codex_limit(""))

    def test_hits_win_even_when_benign_line_also_present(self) -> None:
        pane = BENIGN_LIMIT_STRING + "\n" + REAL_LIMIT_STRING
        self.assertTrue(accounts.detect_codex_limit(pane))

    def test_parses_reset_time(self) -> None:
        parsed = accounts.parse_reset_time(REAL_LIMIT_STRING)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn("2026-07-09T20:36", parsed)

    def test_parses_bare_reset_time_as_today_when_future(self) -> None:
        now = datetime(2026, 7, 9, 19, 30, tzinfo=timezone(timedelta(hours=-4)))
        parsed = accounts.parse_reset_time(
            "You've hit your usage li" "mit; try again at 8:01 PM.",
            now=now,
        )
        self.assertEqual(parsed, "2026-07-09T20:01:00-04:00")

    def test_parses_bare_reset_time_as_tomorrow_when_passed(self) -> None:
        now = datetime(2026, 7, 9, 21, 0, tzinfo=timezone(timedelta(hours=-4)))
        parsed = accounts.parse_reset_time(
            "You've hit your usage li" "mit; try again at 8:01 PM.",
            now=now,
        )
        self.assertEqual(parsed, "2026-07-10T20:01:00-04:00")

    def test_parses_bare_midnight_reset_with_rollover(self) -> None:
        now = datetime(2026, 7, 9, 23, 59, tzinfo=timezone(timedelta(hours=-4)))
        parsed = accounts.parse_reset_time("try again at 12:05 AM", now=now)
        self.assertEqual(parsed, "2026-07-10T00:05:00-04:00")

    def test_reset_time_none_when_missing(self) -> None:
        self.assertIsNone(accounts.parse_reset_time("no reset time here"))

    def test_claude_limit_detection(self) -> None:
        self.assertTrue(accounts.detect_claude_limit(CLAUDE_LIMIT_STRING))
        self.assertFalse(accounts.detect_claude_limit(REAL_LIMIT_STRING))
        self.assertFalse(accounts.detect_claude_limit(""))


class StateRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_fields(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")

            state = accounts.AccountState(
                active="alpha",
                last_rotated_at="2026-01-01T00:00:00+00:00",
                accounts={
                    "alpha": {"limit_reset_at": "2026-07-09T20:36:00-07:00"},
                    "beta": {"limit_reset_at": None},
                },
            )
            accounts.write_state(state)
            reloaded = accounts.read_state()
            self.assertEqual(reloaded.active, "alpha")
            self.assertEqual(reloaded.last_rotated_at, "2026-01-01T00:00:00+00:00")
            self.assertEqual(
                reloaded.accounts["alpha"]["limit_reset_at"], "2026-07-09T20:36:00-07:00"
            )

    def test_ensure_initialized_adopts_first_account(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")

            state = accounts.ensure_state_initialized(accounts.AccountState())
            self.assertEqual(state.active, "alpha")
            self.assertIn("beta", state.accounts)


class PickNextAccountTests(unittest.TestCase):
    def _build_state(self, tmp: Path, names: list[str]) -> accounts.AccountState:
        for name in names:
            d = tmp / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "auth.json").write_text("{}")
        return accounts.ensure_state_initialized(accounts.AccountState())

    def test_round_robin_wraps(self) -> None:
        with _EnvOverride() as paths:
            state = self._build_state(paths["accounts"], ["alpha", "beta", "gamma"])
            state.active = "alpha"
            self.assertEqual(accounts.pick_next_account(state), "beta")
            state.active = "beta"
            self.assertEqual(accounts.pick_next_account(state), "gamma")
            state.active = "gamma"
            self.assertEqual(accounts.pick_next_account(state), "alpha")

    def test_skips_future_reset(self) -> None:
        with _EnvOverride() as paths:
            state = self._build_state(paths["accounts"], ["alpha", "beta", "gamma"])
            state.active = "alpha"
            state.accounts["beta"] = {"limit_reset_at": "2099-01-01T00:00:00+00:00"}
            self.assertEqual(accounts.pick_next_account(state), "gamma")

    def test_no_eligible_returns_none(self) -> None:
        with _EnvOverride() as paths:
            state = self._build_state(paths["accounts"], ["alpha", "beta"])
            state.active = "alpha"
            state.accounts["alpha"] = {"limit_reset_at": "2099-01-01T00:00:00+00:00"}
            state.accounts["beta"] = {"limit_reset_at": "2099-01-01T00:00:00+00:00"}
            self.assertIsNone(accounts.pick_next_account(state))

    def test_past_reset_becomes_eligible(self) -> None:
        with _EnvOverride() as paths:
            state = self._build_state(paths["accounts"], ["alpha", "beta"])
            state.active = "alpha"
            state.accounts["beta"] = {"limit_reset_at": "2000-01-01T00:00:00+00:00"}
            self.assertEqual(accounts.pick_next_account(state), "beta")


class RotationIntegrationTests(unittest.TestCase):
    def _write_registry(self, path: Path, workers: list[dict]) -> None:
        registry = {}
        for worker in workers:
            for key in ("worktree", "cwd"):
                value = worker.get(key)
                if isinstance(value, str) and value != str(Path.home()):
                    Path(value).mkdir(parents=True, exist_ok=True)
            registry[worker["ticket"]] = {"current": worker}
        path.write_text(json.dumps(registry))

    def test_full_rotation_swaps_auth_snapshots_outgoing_and_revives(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text('{"tokens":"alpha-v1"}')
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text('{"tokens":"beta-v1"}')

            paths["auth"].write_text('{"tokens":"alpha-refreshed"}')

            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    },
                    {
                        "ticket": "WIKI-99",
                        "window": "@43",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-99"),
                        "log": "/tmp/cdx-WIKI-99.log",
                        "session_id": "sess-wiki-99",
                    },
                    {
                        "ticket": "WIKI-14",
                        "window": "@44",
                        "kind": "cc",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-14"),
                        "log": "/tmp/cc-WIKI-14.log",
                    },
                ],
            )

            state = accounts.ensure_state_initialized(accounts.AccountState())
            self.assertEqual(state.active, "alpha")

            killed: list[str] = []
            revived: list[tuple[str, str, str]] = []
            wiki_updates: list[tuple[str, str, str, str | None]] = []

            def fake_live() -> set[str]:
                return {"@42", "@43", "@44"}

            def fake_kill(window: str) -> None:
                killed.append(window)

            counter = {"n": 100}

            def fake_new(name: str, cwd: str, command: str, target_session: str | None = None) -> str:
                counter["n"] += 1
                revived.append((name, cwd, command))
                return f"@{counter['n']}"

            def fake_pipe(window: str, log_path: str) -> None:
                pass

            def fake_send(window: str, text: str) -> None:
                pass

            def fake_ready(window: str, timeout: float) -> bool:
                return True

            def fake_wiki_update(
                ticket: str,
                window: str,
                log: str,
                session_id: str | None = None,
            ) -> tuple[bool, None]:
                wiki_updates.append((ticket, window, log, session_id))
                return True, None

            with mock.patch.object(accounts, "tmux_live_windows", fake_live), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(accounts, "tmux_new_window", fake_new), \
                 mock.patch.object(accounts, "tmux_pipe_pane", fake_pipe), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", fake_send), \
                 mock.patch.object(accounts, "wait_for_codex_ready", fake_ready), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", fake_wiki_update), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: f"sess-{t.lower()}"):
                result = accounts.rotate(
                    state=state,
                    outgoing_reset_at="2099-01-01T00:00:00+00:00",
                )

            self.assertEqual(sorted(killed), ["@42", "@43"])
            self.assertEqual(result.outgoing, "alpha")
            self.assertEqual(result.incoming, "beta")
            self.assertEqual(sorted(result.revived), ["WIKI-15", "WIKI-99"])

            # Outgoing account preserved the refreshed token.
            outgoing_snapshot = json.loads((paths["accounts"] / "alpha" / "auth.json").read_text())
            self.assertEqual(outgoing_snapshot["tokens"], "alpha-refreshed")

            # ~/.codex/auth.json now holds beta's credentials.
            new_active = json.loads(paths["auth"].read_text())
            self.assertEqual(new_active["tokens"], "beta-v1")

            # State file updated.
            reloaded = accounts.read_state()
            self.assertEqual(reloaded.active, "beta")
            self.assertEqual(
                reloaded.accounts["alpha"]["limit_reset_at"], "2099-01-01T00:00:00+00:00"
            )
            self.assertIsNone(reloaded.accounts["beta"]["limit_reset_at"])
            self.assertIsNotNone(reloaded.last_rotated_at)

            # Revival: cwd = worktree, command = codex resume <session_id>, wiki update called.
            self.assertEqual(len(revived), 2)
            for name, cwd, command in revived:
                self.assertTrue(name.startswith("cdx:WIKI-"))
                self.assertTrue(command.startswith("codex resume "))
                self.assertNotIn("--last", command, "explicit-id-only: no --last fallback")
                self.assertTrue(cwd.startswith(str(paths["root"])))
            self.assertEqual(len(wiki_updates), 2)
            for ticket, window, log, session_id in wiki_updates:
                self.assertTrue(ticket.startswith("WIKI-"))
                self.assertTrue(window.startswith("@"))
                self.assertIn("-r1.log", log)
                self.assertTrue(session_id and session_id.startswith("sess-"))

            # Rotation log has one JSON line, no credential contents.
            log_content = paths["rotation_log"].read_text().strip().splitlines()
            self.assertEqual(len(log_content), 1)
            entry = json.loads(log_content[0])
            self.assertEqual(entry["from"], "alpha")
            self.assertEqual(entry["to"], "beta")
            self.assertNotIn("tokens", json.dumps(entry))

    def test_rotation_prefers_session_id_when_rollout_matches(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")

            # cwd basename must equal the ticket slug ("wiki-15") for the cwd
            # match branch of find_session_id_for_worker.
            worktree = paths["root"] / "wiki-15"
            worktree.mkdir()
            now = datetime.now(tz=timezone.utc)
            day_dir = paths["sessions"] / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            day_dir.mkdir(parents=True)
            rollout = day_dir / "rollout-abc.jsonl"
            rollout.write_text(
                json.dumps({
                    "payload": {"cwd": str(worktree), "id": "sess-xyz-123"}
                }) + "\n"
            )

            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(worktree),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "spawned_at": now.isoformat(),
                    }
                ],
            )

            state = accounts.ensure_state_initialized(accounts.AccountState())

            commands: list[str] = []

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: (commands.append(cmd), "@200")[1]), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok):
                accounts.rotate(state=state)

            self.assertEqual(commands, ["codex resume sess-xyz-123"])

    def test_revival_rechecks_registry_and_skips_deregistered_ticket(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            new_windows: list[str] = []

            def fake_kill(window: str) -> None:
                self.assertEqual(window, "@42")
                paths["registry"].write_text("{}")

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(
                     accounts,
                     "tmux_new_window",
                     lambda n, c, cmd, target_session=None: new_windows.append(cmd) or "@200",
                 ), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertEqual(new_windows, [], "deregistered tickets must not be revived")
            self.assertEqual(result.revived, [])
            self.assertEqual(result.failed, ["WIKI-15"])
            self.assertIn("no longer in the registry", result.failed_reasons["WIKI-15"])

    def test_revival_skips_terminal_history_entry(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            worker = {
                "ticket": "WIKI-15",
                "window": "@42",
                "kind": "cdx",
                "role": "implement",
                "worktree": str(paths["root"] / "wt-15"),
                "log": "/tmp/cdx-WIKI-15.log",
                "session_id": "sess-wiki-15",
            }
            self._write_registry(paths["registry"], [worker])
            state = accounts.ensure_state_initialized(accounts.AccountState())

            def fake_kill(window: str) -> None:
                archived = dict(worker)
                archived["outcome"] = "merged"
                paths["registry"].write_text(json.dumps({
                    "WIKI-15": {"history": [archived]},
                }))

            new_windows: list[str] = []
            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(
                     accounts,
                     "tmux_new_window",
                     lambda n, c, cmd, target_session=None: new_windows.append(cmd) or "@200",
                 ), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertEqual(new_windows, [])
            self.assertEqual(result.failed, ["WIKI-15"])
            self.assertIn("terminal registry outcome", result.failed_reasons["WIKI-15"])

    def test_revival_uses_registry_worktree_reloaded_after_kill(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            old_worktree = paths["root"] / "old-wt"
            new_worktree = paths["root"] / "new-wt"
            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(old_worktree),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            cwd_used: list[str] = []

            def fake_kill(window: str) -> None:
                self._write_registry(
                    paths["registry"],
                    [
                        {
                            "ticket": "WIKI-15",
                            "window": "@42",
                            "kind": "cdx",
                            "role": "implement",
                            "worktree": str(new_worktree),
                            "log": "/tmp/cdx-WIKI-15.log",
                            "session_id": "sess-wiki-15",
                        }
                    ],
                )

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(
                     accounts,
                     "tmux_new_window",
                     lambda n, c, cmd, target_session=None: cwd_used.append(c) or "@200",
                 ), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertEqual(result.revived, ["WIKI-15"])
            self.assertEqual(cwd_used, [str(new_worktree)])

    def test_revival_falls_back_to_registry_cwd_and_refuses_home(self) -> None:
        with _EnvOverride() as paths:
            fallback_cwd = paths["root"] / "cwd-only"
            worker = {
                "ticket": "WIKI-15",
                "window": "@42",
                "kind": "cdx",
                "role": "implement",
                "cwd": str(fallback_cwd),
                "log": "/tmp/cdx-WIKI-15.log",
                "session_id": "sess-wiki-15",
            }
            revived_worker, reason = accounts._worker_from_registry_entry(
                "WIKI-15", {"current": worker}, "cdx"
            )
            self.assertIsNone(reason)
            self.assertIsNotNone(revived_worker)
            assert revived_worker is not None
            self.assertEqual(revived_worker.worktree, str(fallback_cwd))

            home_worker = dict(worker)
            home_worker["cwd"] = str(Path.home())
            revived_worker, reason = accounts._worker_from_registry_entry(
                "WIKI-15", {"current": home_worker}, "cdx"
            )
            self.assertIsNone(revived_worker)
            self.assertIn("$HOME", reason or "")

            pruned_worker = dict(worker)
            pruned_worker["cwd"] = str(paths["root"] / "missing-worktree")
            revived_worker, reason = accounts._worker_from_registry_entry(
                "WIKI-15",
                {"current": pruned_worker},
                "cdx",
                require_existing_cwd=True,
            )
            self.assertIsNone(revived_worker)
            self.assertIn("does not exist", reason or "")

    def test_wiki_agent_update_receives_post_resume_session_id(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-before",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            commands: list[str] = []
            updates: list[tuple[str, str | None]] = []

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(
                     accounts,
                     "tmux_new_window",
                     lambda n, c, cmd, target_session=None: commands.append(cmd) or "@200",
                 ), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(
                     accounts,
                     "wiki_agent_update",
                     lambda ticket, window, log_path, sid=None: updates.append((ticket, sid)) or (True, None),
                 ), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-after"), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                accounts.rotate(state=state)

            self.assertEqual(commands, ["codex resume sess-before"])
            self.assertEqual(updates, [("WIKI-15", "sess-after")])

    def test_post_resume_session_rescan_failure_marks_revival_failed(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-before",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            kill_calls: list[str] = []
            update_calls: list[str] = []

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: kill_calls.append(w)), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", lambda ticket, window, log_path, sid=None: update_calls.append(ticket) or (True, None)), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: None), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertEqual(result.revived, [])
            self.assertEqual(result.failed, ["WIKI-15"])
            self.assertIn("post-resume session id unresolved", result.failed_reasons["WIKI-15"])
            self.assertEqual(kill_calls, ["@42", "@200"])
            self.assertEqual(update_calls, [])

    def test_wiki_agent_update_failure_marks_revival_failed(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-before",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            kill_calls: list[str] = []

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: kill_calls.append(w)), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", lambda ticket, window, log_path, sid=None: (False, "registry locked")), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-after"), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertEqual(result.revived, [])
            self.assertEqual(result.failed, ["WIKI-15"])
            self.assertIn("wiki agent update failed: registry locked", result.failed_reasons["WIKI-15"])
            self.assertEqual(kill_calls, ["@42", "@200"])

    def test_missing_original_tmux_session_marks_revival_failed(self) -> None:
        with _EnvOverride() as paths:
            worker = accounts.WorkerEntry(
                ticket="WIKI-15",
                window="@42",
                worktree=str(paths["root"]),
                log="/tmp/cdx-WIKI-15.log",
                kind="cdx",
                role="implement",
                orch=None,
                session_id="sess-before",
            )
            new_windows: list[str] = []

            with mock.patch.object(
                accounts,
                "tmux_new_window",
                lambda n, c, cmd, target_session=None: new_windows.append(cmd) or "@200",
            ):
                new_window, reason = accounts._revive_worker(worker, target_session=None)

            self.assertIsNone(new_window)
            self.assertIn("original tmux session unavailable", reason or "")
            self.assertEqual(new_windows, [])

    def test_login_status_failure_aborts_before_revive(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text('{"tokens":"alpha-v1"}')
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text('{"tokens":"beta-v1"}')
            paths["auth"].write_text('{"tokens":"alpha-refreshed"}')

            self._write_registry(
                paths["registry"],
                [
                    {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                    }
                ],
            )
            state = accounts.ensure_state_initialized(accounts.AccountState())

            new_windows: list[str] = []
            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: new_windows.append(cmd) or "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "codex_login_status", lambda: False):
                with self.assertRaises(accounts.RotationError):
                    accounts.rotate(state=state)

            self.assertEqual(new_windows, [], "no worker may be revived onto unverified creds")
            # Outgoing snapshot preserved AND ~/.codex/auth.json restored to it.
            active_auth = json.loads(paths["auth"].read_text())
            self.assertEqual(active_auth["tokens"], "alpha-refreshed")
            reloaded = accounts.read_state()
            self.assertEqual(reloaded.active, "alpha")

    def test_no_eligible_raises(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")

            state = accounts.ensure_state_initialized(accounts.AccountState())
            with self.assertRaises(accounts.NoEligibleAccountError):
                accounts.rotate(state=state)

    def test_manual_rotation_does_not_pin_unknown_outgoing_reset(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            paths["registry"].write_text("{}")

            state = accounts.ensure_state_initialized(accounts.AccountState())

            with mock.patch.object(accounts, "tmux_live_windows", set), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                result = accounts.rotate(state=state)

            self.assertIsNone(result.reset_at)
            reloaded = accounts.read_state()
            self.assertIsNone(reloaded.accounts["alpha"]["limit_reset_at"])


class CredentialRotationTests(unittest.TestCase):
    def _seed(self, paths: dict[str, Path]) -> accounts.AccountState:
        for name, token in (("alpha", "alpha-stored"), ("beta", "beta")):
            account_dir = paths["accounts"] / name
            account_dir.mkdir()
            (account_dir / "auth.json").write_text(
                json.dumps({"tokens": token}), encoding="utf-8"
            )
        paths["auth"].write_text(
            json.dumps({"tokens": "alpha-refreshed"}), encoding="utf-8"
        )
        state = accounts.AccountState(
            active="alpha",
            last_rotated_at="2026-07-01T12:00:00+00:00",
            accounts={
                "alpha": {"limit_reset_at": None, "label": "primary"},
                "beta": {"limit_reset_at": None, "label": "backup"},
            },
        )
        accounts.write_state(state)
        return state

    def test_success_uses_preexisting_snapshot_without_tmux(self) -> None:
        with _EnvOverride() as paths:
            state = self._seed(paths)
            refreshed = paths["auth"].read_bytes()
            (paths["accounts"] / "alpha" / "auth.json").write_bytes(refreshed)

            tmux_called = AssertionError(
                "credential-only rotation must not invoke tmux"
            )
            with mock.patch.object(
                accounts, "snapshot_active_auth", side_effect=AssertionError(
                    "caller already snapshotted outgoing auth"
                )
            ), mock.patch.object(
                accounts, "iter_workers", side_effect=tmux_called
            ), mock.patch.object(
                accounts, "tmux_live_windows", side_effect=tmux_called
            ), mock.patch.object(
                accounts, "tmux_kill_window", side_effect=tmux_called
            ), mock.patch.object(
                accounts, "tmux_new_window", side_effect=tmux_called
            ), mock.patch.object(accounts, "codex_login_status", return_value=True):
                result = accounts.rotate_credentials(
                    state=state,
                    outgoing_reset_at="2099-01-01T00:00:00+00:00",
                    snapshot_outgoing=False,
                )

            self.assertEqual(result.outgoing, "alpha")
            self.assertEqual(result.incoming, "beta")
            self.assertEqual(
                json.loads(paths["auth"].read_text(encoding="utf-8"))["tokens"],
                "beta",
            )
            self.assertEqual(
                (paths["accounts"] / "alpha" / "auth.json").read_bytes(),
                refreshed,
            )
            self.assertEqual(state.active, "beta")
            self.assertEqual(
                state.accounts["alpha"]["limit_reset_at"],
                "2099-01-01T00:00:00+00:00",
            )
            self.assertEqual(accounts.read_state().to_dict(), state.to_dict())

    def test_login_failure_restores_auth_and_state(self) -> None:
        with _EnvOverride() as paths:
            state = self._seed(paths)
            auth_before = paths["auth"].read_bytes()
            state_before = state.to_dict()
            state_file_before = (paths["accounts"] / "state.json").read_bytes()

            with mock.patch.object(accounts, "codex_login_status", return_value=False):
                with self.assertRaisesRegex(
                    accounts.RotationError, "codex login status failed"
                ):
                    accounts.rotate_credentials(
                        state=state,
                        outgoing_reset_at="2099-01-01T00:00:00+00:00",
                    )

            self.assertEqual(paths["auth"].read_bytes(), auth_before)
            self.assertEqual(state.to_dict(), state_before)
            self.assertEqual(
                (paths["accounts"] / "state.json").read_bytes(), state_file_before
            )
            self.assertEqual(accounts.read_state().to_dict(), state_before)
            self.assertEqual(
                json.loads(
                    (paths["accounts"] / "alpha" / "auth.json").read_text(
                        encoding="utf-8"
                    )
                )["tokens"],
                "alpha-refreshed",
            )

    def test_write_state_failure_restores_auth_and_both_state_views(self) -> None:
        with _EnvOverride() as paths:
            state = self._seed(paths)
            auth_before = paths["auth"].read_bytes()
            state_before = state.to_dict()
            state_path = paths["accounts"] / "state.json"
            state_file_before = state_path.read_bytes()
            real_write_state = accounts.write_state

            def write_then_fail(candidate: accounts.AccountState) -> None:
                real_write_state(candidate)
                raise OSError("simulated post-replace failure")

            with mock.patch.object(accounts, "codex_login_status", return_value=True), \
                 mock.patch.object(accounts, "write_state", side_effect=write_then_fail):
                with self.assertRaisesRegex(OSError, "post-replace failure"):
                    accounts.rotate_credentials(
                        state=state,
                        outgoing_reset_at="2099-01-01T00:00:00+00:00",
                    )

            self.assertEqual(paths["auth"].read_bytes(), auth_before)
            self.assertEqual(state.to_dict(), state_before)
            self.assertEqual(state_path.read_bytes(), state_file_before)
            self.assertEqual(accounts.read_state().to_dict(), state_before)


class ConcurrentRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_rotate_locked_debounces(self) -> None:
        """Two concurrent rotate_locked calls must not double-swap: the second
        one, arriving after the first commits, sees a fresh last_rotated_at
        and raises RotationDebouncedError."""
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text('{"tokens":"alpha"}')
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text('{"tokens":"beta"}')
            paths["auth"].write_text('{"tokens":"alpha"}')
            paths["registry"].write_text("{}")

            state = accounts.ensure_state_initialized(accounts.AccountState())

            kills: list[str] = []
            with mock.patch.object(accounts, "tmux_live_windows", set), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: kills.append(w)), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True):
                first, second = await asyncio.gather(
                    accounts.rotate_locked(state=state, respect_debounce=False),
                    accounts.rotate_locked(state=state, respect_debounce=True),
                    return_exceptions=True,
                )

            successes = [r for r in (first, second) if isinstance(r, accounts.RotationResult)]
            debounced = [r for r in (first, second) if isinstance(r, accounts.RotationDebouncedError)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(debounced), 1)

    async def test_watchdog_debounces_against_persisted_state(self) -> None:
        """A restart-fresh watchdog (in-memory counter at 0) must still honor
        the persisted last_rotated_at and skip if it's inside the window."""
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")

            paths["registry"].write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                    }
                }
            }))

            # Pretend the previous backend rotated 60s ago — well within the
            # 600s default debounce.
            recent = datetime.now(timezone.utc).isoformat()
            accounts.write_state(accounts.AccountState(
                active="alpha",
                last_rotated_at=recent,
                accounts={"alpha": {"limit_reset_at": None}, "beta": {"limit_reset_at": None}},
            ))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_LIMIT_STRING):
                watch = accounts.WatchdogInternalState()  # in-memory counter fresh
                await accounts._check_once(watch, emit)

            self.assertEqual(emitted, [], "persisted debounce must block the rotation")
            self.assertEqual(accounts.read_state().active, "alpha", "no swap should have happened")


def _needs_timezone_import() -> None:
    """Guard: the test above uses datetime.now(timezone.utc)."""
    from datetime import timezone  # noqa: F401


class WatchdogLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_once_debounces_within_window(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            (paths["root"] / "wt-15").mkdir()

            registry = paths["registry"]
            registry.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                }
            }))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_LIMIT_STRING), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)
                await accounts._check_once(watch, emit)

            self.assertEqual(len(emitted), 1, "second call within debounce must be a no-op")
            self.assertEqual(emitted[0]["type"], "codex_rotation")
            self.assertEqual(emitted[0]["to"], "beta")

    async def test_no_eligible_emits_alert(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")

            registry = paths["registry"]
            registry.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                    }
                }
            }))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_LIMIT_STRING):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)

            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["type"], "codex_limit_no_eligible")
            self.assertIn("WIKI-15", emitted[0]["tickets"])

    async def test_limit_rotation_pins_fallback_reset_when_parse_missing(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            (paths["root"] / "wt-15").mkdir()
            paths["registry"].write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                }
            }))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: "■ You've hit your usage li" "mit."), \
                 mock.patch.object(accounts, "_fallback_reset_time", lambda: "2099-01-01T00:00:00+00:00"), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)

            self.assertEqual(emitted[0]["type"], "codex_rotation")
            self.assertEqual(
                accounts.read_state().accounts["alpha"]["limit_reset_at"],
                "2099-01-01T00:00:00+00:00",
            )

    async def test_claude_hit_alerts_only(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            registry = paths["registry"]
            registry.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@44",
                        "kind": "cc",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cc-WIKI-15.log",
                    }
                }
            }))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@44"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: CLAUDE_LIMIT_STRING):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)

            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["type"], "claude_limit_hit")


REAL_AUTH_DEAD_STRING = (
    "Your access token could not be refreshed because you have since logged "
    "out or signed in to another account. Please sign in again."
)
CWD_DIALOG_STRING = (
    "1. Use original directory\n"
    "2. Use current directory (/Users/henry/me/fun/wiki/.claude/worktrees/wiki-19)\n"
    "Press enter to continue"
)


def _write_rollout(day_dir: Path, name: str, cwd: str | None, session_id: str,
                   kickoff_ticket: str | None = None, mtime: float | None = None,
                   pad_bytes: int = 0) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{name}.jsonl"
    lines = [json.dumps({"payload": {"cwd": cwd, "id": session_id}})]
    if kickoff_ticket:
        lines.append(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"You are worker for ticket {kickoff_ticket}. Do the work.",
            },
        }))
    if pad_bytes:
        lines.append(json.dumps({"pad": "x" * pad_bytes}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class AuthDeadDetectionTests(unittest.TestCase):
    def test_matches_the_real_auth_dead_string(self) -> None:
        self.assertTrue(accounts.detect_codex_auth_dead(REAL_AUTH_DEAD_STRING))

    def test_ignores_bare_sign_in_again_phrase(self) -> None:
        """Product copy, docs, and test output frequently contain "please sign
        in again"; matching it as an independent alternative would kill+resume
        any worker merely displaying the phrase every poll. Only the full
        "your access token could not be refreshed" sentence classifies."""
        self.assertFalse(accounts.detect_codex_auth_dead("Please sign in again to continue."))
        self.assertFalse(accounts.detect_codex_auth_dead(
            "docs say: If prompted, please sign in again with your credentials."
        ))
        self.assertFalse(accounts.detect_codex_auth_dead("something failed. Please sign in again."))

    def test_ignores_unrelated_pane(self) -> None:
        self.assertFalse(accounts.detect_codex_auth_dead("everything is fine"))
        self.assertFalse(accounts.detect_codex_auth_dead(""))

    def test_auth_dead_is_not_a_limit_hit(self) -> None:
        self.assertFalse(accounts.detect_codex_limit(REAL_AUTH_DEAD_STRING))

    def test_cwd_dialog_signature(self) -> None:
        self.assertTrue(accounts.detect_cwd_dialog(CWD_DIALOG_STRING))
        self.assertFalse(accounts.detect_cwd_dialog("no dialog here"))


class SessionIdResolutionTests(unittest.TestCase):
    def _prepare(self, paths: dict[str, Path]) -> tuple[Path, Path]:
        now = datetime.now(tz=timezone.utc)
        day_dir = paths["sessions"] / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
        worktree = paths["root"] / "wiki-15"
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree, day_dir

    def test_kickoff_ticket_match_returns_id(self) -> None:
        with _EnvOverride() as paths:
            worktree, day_dir = self._prepare(paths)
            _write_rollout(day_dir, "kick", cwd="/somewhere/else", session_id="sess-K",
                           kickoff_ticket="WIKI-15", mtime=time.time())
            sid = accounts.find_session_id_for_worker(
                "WIKI-15", str(worktree), datetime.now(tz=timezone.utc).isoformat()
            )
            self.assertEqual(sid, "sess-K")

    def test_cwd_slug_match_returns_id(self) -> None:
        with _EnvOverride() as paths:
            worktree, day_dir = self._prepare(paths)
            _write_rollout(day_dir, "cwd", cwd=str(worktree), session_id="sess-C",
                           mtime=time.time())
            sid = accounts.find_session_id_for_worker(
                "WIKI-15", str(worktree), datetime.now(tz=timezone.utc).isoformat()
            )
            self.assertEqual(sid, "sess-C")

    def test_newest_mtime_wins_on_tie(self) -> None:
        with _EnvOverride() as paths:
            worktree, day_dir = self._prepare(paths)
            now = time.time()
            _write_rollout(day_dir, "old", cwd=str(worktree), session_id="sess-old",
                           kickoff_ticket="WIKI-15", mtime=now - 3600)
            _write_rollout(day_dir, "new", cwd=str(worktree), session_id="sess-new",
                           kickoff_ticket="WIKI-15", mtime=now)
            sid = accounts.find_session_id_for_worker(
                "WIKI-15", str(worktree), datetime.now(tz=timezone.utc).isoformat()
            )
            self.assertEqual(sid, "sess-new")

    def test_size_breaks_mtime_tie(self) -> None:
        with _EnvOverride() as paths:
            worktree, day_dir = self._prepare(paths)
            same_time = time.time()
            _write_rollout(day_dir, "small", cwd=str(worktree), session_id="sess-small",
                           kickoff_ticket="WIKI-15", mtime=same_time)
            _write_rollout(day_dir, "large", cwd=str(worktree), session_id="sess-large",
                           kickoff_ticket="WIKI-15", mtime=same_time, pad_bytes=4096)
            sid = accounts.find_session_id_for_worker(
                "WIKI-15", str(worktree), datetime.now(tz=timezone.utc).isoformat()
            )
            self.assertEqual(sid, "sess-large")

    def test_no_match_returns_none(self) -> None:
        with _EnvOverride() as paths:
            worktree, day_dir = self._prepare(paths)
            _write_rollout(day_dir, "other", cwd="/nope", session_id="sess-nope",
                           kickoff_ticket="WIKI-999", mtime=time.time())
            sid = accounts.find_session_id_for_worker(
                "WIKI-15", str(worktree), datetime.now(tz=timezone.utc).isoformat()
            )
            self.assertIsNone(sid)


class NoLineageNoFreshSpawnTests(unittest.TestCase):
    """When a worker has no resolvable rollout AND no registry session_id, we
    must NOT spawn a fresh session (which would replay no history and orphan
    the transcript). The rotation succeeds; the worker is failed with a reason."""

    def test_worker_without_lineage_is_skipped_and_reason_recorded(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            (paths["root"] / "wt-15").mkdir()

            registry = paths["registry"]
            registry.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                    }
                }
            }))
            state = accounts.ensure_state_initialized(accounts.AccountState())

            new_windows: list[str] = []
            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: new_windows.append(cmd) or "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: None):
                result = accounts.rotate(state=state)

            self.assertEqual(new_windows, [], "no fresh spawn without lineage")
            self.assertEqual(result.revived, [])
            self.assertEqual(result.failed, ["WIKI-15"])
            self.assertIn("WIKI-15", result.failed_reasons)
            self.assertIn("manual attention", result.failed_reasons["WIKI-15"])


class CwdDialogAutoAnswerTests(unittest.TestCase):
    def test_answers_2_and_enter_when_dialog_visible(self) -> None:
        captured_panes = iter([CWD_DIALOG_STRING])
        send_calls: list[list[str]] = []

        def fake_capture(window: str, lines: int = 40) -> str:
            try:
                return next(captured_panes)
            except StopIteration:
                return ""

        def fake_run(args, **kwargs):
            send_calls.append(list(args))
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        with mock.patch.object(accounts, "tmux_capture", fake_capture), \
             mock.patch("backend.app.accounts.subprocess.run", fake_run):
            answered = accounts.wait_for_cwd_dialog_and_answer("@42", timeout_seconds=1.0)

        self.assertTrue(answered)
        self.assertTrue(any(
            "send-keys" in " ".join(call) and call[-2:] == ["2", "Enter"]
            for call in send_calls
        ))

    def test_no_dialog_returns_false(self) -> None:
        with mock.patch.object(accounts, "tmux_capture", lambda w, lines=40: "nothing here"):
            answered = accounts.wait_for_cwd_dialog_and_answer("@42", timeout_seconds=0.5)
        self.assertFalse(answered)


class SessionPreservationTests(unittest.TestCase):
    """Revival window must spawn into the ORIGINAL tmux session, captured
    before the dying window is killed."""

    def test_new_window_receives_target_session_from_dying_window(self) -> None:
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            (paths["root"] / "wt-15").mkdir()

            registry = paths["registry"]
            registry.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                }
            }))
            state = accounts.ensure_state_initialized(accounts.AccountState())

            session_calls: list[str] = []
            new_window_calls: list[dict] = []
            kill_calls: list[str] = []

            def fake_session(window: str) -> str:
                session_calls.append(window)
                return "phoebe" if window == "@42" else "wiki"

            def fake_kill(window: str) -> None:
                kill_calls.append(window)

            def fake_new(name: str, cwd: str, command: str,
                         target_session: str | None = None) -> str:
                new_window_calls.append({"name": name, "target_session": target_session})
                return "@200"

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_window_session", fake_session), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(accounts, "tmux_new_window", fake_new), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
                accounts.rotate(state=state)

            # Session was captured BEFORE kill.
            self.assertEqual(session_calls, ["@42"], "session lookup once per worker, before kill")
            self.assertEqual(kill_calls, ["@42"])
            self.assertEqual(len(new_window_calls), 1)
            self.assertEqual(new_window_calls[0]["target_session"], "phoebe")


class AuthDeadRevivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_dead_worker_is_revived_without_rotation(self) -> None:
        """Auth-dead workers get killed + resumed on CURRENT auth.json — no
        account swap. The rotation loop must not treat them as limit hits."""
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text('{"tokens":"alpha"}')
            (paths["accounts"] / "beta").mkdir()
            (paths["accounts"] / "beta" / "auth.json").write_text('{"tokens":"beta"}')
            paths["auth"].write_text('{"tokens":"alpha-live"}')
            (paths["root"] / "wt-15").mkdir()

            paths["registry"].write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                }
            }))
            state = accounts.ensure_state_initialized(accounts.AccountState())
            self.assertEqual(state.active, "alpha")

            emitted: list[dict] = []
            revive_commands: list[str] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_AUTH_DEAD_STRING), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: revive_commands.append(cmd) or "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)

            # Revival happened.
            self.assertEqual(len(revive_commands), 1)
            self.assertIn("codex resume sess-wiki-15", revive_commands[0])

            # Auth-dead SSE event emitted; NO rotation event.
            types = [e["type"] for e in emitted]
            self.assertIn("codex_auth_dead_revival", types)
            self.assertNotIn("codex_rotation", types)

            # Active account unchanged (still alpha) — no swap.
            self.assertEqual(accounts.read_state().active, "alpha")

            # auth.json untouched (still alpha's live creds).
            self.assertEqual(json.loads(paths["auth"].read_text())["tokens"], "alpha-live")


class AuthDeadAttemptCapTests(unittest.IsolatedAsyncioTestCase):
    """A genuinely-dead auth token re-shows the pane signature after every
    revive. Without a cap, the watchdog kill+resume-loops the same worker
    every poll cycle forever."""

    async def _drive_cycles(
        self,
        paths: dict[str, Path],
        cycles: int,
    ) -> tuple[list[dict], list[str]]:
        (paths["accounts"] / "alpha").mkdir()
        (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
        paths["auth"].write_text("{}")
        (paths["root"] / "wt-15").mkdir()
        paths["registry"].write_text(json.dumps({
            "WIKI-15": {
                "current": {
                    "ticket": "WIKI-15",
                    "window": "@42",
                    "kind": "cdx",
                    "role": "implement",
                    "worktree": str(paths["root"] / "wt-15"),
                    "log": "/tmp/cdx-WIKI-15.log",
                    "session_id": "sess-wiki-15",
                }
            }
        }))
        accounts.ensure_state_initialized(accounts.AccountState())

        emitted: list[dict] = []
        revive_calls: list[str] = []

        async def emit(evt: dict) -> None:
            emitted.append(evt)

        with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
             mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_AUTH_DEAD_STRING), \
             mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
             mock.patch.object(accounts, "tmux_kill_window", lambda w: revive_calls.append(f"kill:{w}")), \
             mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: (revive_calls.append(f"new:{cmd}"), "@200")[1]), \
             mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
             mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
             mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
             mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
             mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
             mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
            watch = accounts.WatchdogInternalState()
            for _ in range(cycles):
                # Bypass the cooldown between cycles by rewinding the last
                # attempt timestamp past the cooldown boundary.
                for ticket in list(watch.auth_dead_attempts.keys()):
                    history = watch.auth_dead_attempts[ticket]
                    if history:
                        history[-1] -= accounts.AUTH_DEAD_COOLDOWN_SECONDS + 1
                await accounts._check_once(watch, emit)
        return emitted, revive_calls

    async def test_stops_reviving_after_max_attempts(self) -> None:
        with _EnvOverride() as paths:
            emitted, calls = await self._drive_cycles(paths, cycles=5)
        revive_events = [e for e in emitted if e["type"] == "codex_auth_dead_revival"]
        exhausted_events = [e for e in emitted if e["type"] == "codex_auth_dead_exhausted"]
        self.assertEqual(
            len(revive_events),
            accounts.AUTH_DEAD_MAX_ATTEMPTS,
            "revive at most AUTH_DEAD_MAX_ATTEMPTS times",
        )
        self.assertGreaterEqual(
            len(exhausted_events),
            1,
            "manual-attention alert emitted after cap hit",
        )
        # Once exhausted, no further new-window calls.
        new_window_calls = [c for c in calls if c.startswith("new:")]
        self.assertEqual(len(new_window_calls), accounts.AUTH_DEAD_MAX_ATTEMPTS)

    async def test_cooldown_blocks_back_to_back_revives(self) -> None:
        """Two consecutive polls (no timestamp rewind) → only ONE revive."""
        with _EnvOverride() as paths:
            (paths["accounts"] / "alpha").mkdir()
            (paths["accounts"] / "alpha" / "auth.json").write_text("{}")
            paths["auth"].write_text("{}")
            (paths["root"] / "wt-15").mkdir()
            paths["registry"].write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "ticket": "WIKI-15",
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-15"),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session_id": "sess-wiki-15",
                    }
                }
            }))
            accounts.ensure_state_initialized(accounts.AccountState())

            emitted: list[dict] = []
            new_calls: list[str] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_AUTH_DEAD_STRING), \
                 mock.patch.object(accounts, "tmux_window_session", lambda w: "phoebe"), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd, target_session=None: (new_calls.append(cmd), "@200")[1]), \
                 mock.patch.object(accounts, "tmux_pipe_pane", _ignore_pipe_pane), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wait_for_cwd_dialog_and_answer", lambda w, timeout_seconds=8.0: False), \
                 mock.patch.object(accounts, "wiki_agent_update", _wiki_agent_update_ok), \
                 mock.patch.object(accounts, "find_session_id_for_worker", lambda t, wt, sa: "sess-wiki-15"):
                watch = accounts.WatchdogInternalState()
                await accounts._check_once(watch, emit)
                await accounts._check_once(watch, emit)

            self.assertEqual(
                len(new_calls),
                1,
                "cooldown must block a second revive inside the window",
            )


class RevivalMessageTests(unittest.TestCase):
    def test_message_includes_ticket_and_status_file(self) -> None:
        msg = accounts.revival_message("WIKI-42")
        self.assertIn("WIKI-42", msg)
        self.assertIn("/tmp/agent-status/WIKI-42.json", msg)
        self.assertIn("worker for ticket", msg.lower())


if __name__ == "__main__":
    unittest.main()
