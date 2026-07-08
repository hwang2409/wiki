"""Unit + integration tests for the codex account watchdog + rotation.

Every path referenced here is a temp dir. Never touches real credentials.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import accounts


REAL_LIMIT_STRING = (
    "■ You've hit your usage limit. Visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at Jul 9th, 2026 8:36 PM."
)
BENIGN_LIMIT_STRING = (
    "usage limit resets available. Run /usage for details."
)
CLAUDE_LIMIT_STRING = "Claude usage limit reached. Try again at 4pm."


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


class DetectionTests(unittest.TestCase):
    def test_matches_the_real_codex_limit_string(self) -> None:
        self.assertTrue(accounts.detect_codex_limit(REAL_LIMIT_STRING))

    def test_matches_curly_apostrophe_variant(self) -> None:
        variant = REAL_LIMIT_STRING.replace("You've", "You’ve")
        self.assertTrue(accounts.detect_codex_limit(variant))

    def test_matches_have_variant(self) -> None:
        variant = "You have hit your usage limit."
        self.assertTrue(accounts.detect_codex_limit(variant))

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
        self.assertIn("2026-07-09T20:36", parsed)

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
                    },
                    {
                        "ticket": "WIKI-99",
                        "window": "@43",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(paths["root"] / "wt-99"),
                        "log": "/tmp/cdx-WIKI-99.log",
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
            wiki_updates: list[tuple[str, str, str]] = []

            def fake_live() -> set[str]:
                return {"@42", "@43", "@44"}

            def fake_kill(window: str) -> None:
                killed.append(window)

            counter = {"n": 100}

            def fake_new(name: str, cwd: str, command: str) -> str:
                counter["n"] += 1
                revived.append((name, cwd, command))
                return f"@{counter['n']}"

            def fake_pipe(window: str, log_path: str) -> None:
                pass

            def fake_send(window: str, text: str) -> None:
                pass

            def fake_ready(window: str, timeout: float) -> bool:
                return True

            def fake_wiki_update(ticket: str, window: str, log: str) -> None:
                wiki_updates.append((ticket, window, log))

            with mock.patch.object(accounts, "tmux_live_windows", fake_live), \
                 mock.patch.object(accounts, "tmux_kill_window", fake_kill), \
                 mock.patch.object(accounts, "tmux_new_window", fake_new), \
                 mock.patch.object(accounts, "tmux_pipe_pane", fake_pipe), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", fake_send), \
                 mock.patch.object(accounts, "wait_for_codex_ready", fake_ready), \
                 mock.patch.object(accounts, "wiki_agent_update", fake_wiki_update), \
                 mock.patch.object(accounts, "find_session_id_for_worktree", lambda w: None):
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

            # Revival: cwd = worktree, command = codex resume --last, wiki update called.
            self.assertEqual(len(revived), 2)
            for name, cwd, command in revived:
                self.assertTrue(name.startswith("cdx:WIKI-"))
                self.assertTrue(command.startswith("codex resume"))
                self.assertTrue(cwd.startswith(str(paths["root"])))
            self.assertEqual(len(wiki_updates), 2)
            for ticket, window, log in wiki_updates:
                self.assertTrue(ticket.startswith("WIKI-"))
                self.assertTrue(window.startswith("@"))
                self.assertIn("-r1.log", log)

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

            worktree = paths["root"] / "wt-15"
            worktree.mkdir()
            rollout_dir = paths["sessions"] / "2026" / "07"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / "rollout-abc.jsonl"
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
                    }
                ],
            )

            state = accounts.ensure_state_initialized(accounts.AccountState())

            commands: list[str] = []

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd: (commands.append(cmd), "@200")[1]), \
                 mock.patch.object(accounts, "tmux_pipe_pane", lambda w, l: None), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "wiki_agent_update", lambda t, w, l: None):
                accounts.rotate(state=state)

            self.assertEqual(commands, ["codex resume sess-xyz-123"])

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
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd: new_windows.append(cmd) or "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", lambda w, l: None), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wiki_agent_update", lambda t, w, l: None), \
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
                    }
                }
            }))

            emitted: list[dict] = []

            async def emit(evt: dict) -> None:
                emitted.append(evt)

            with mock.patch.object(accounts, "tmux_live_windows", lambda: {"@42"}), \
                 mock.patch.object(accounts, "tmux_capture", lambda w, lines=60: REAL_LIMIT_STRING), \
                 mock.patch.object(accounts, "tmux_kill_window", lambda w: None), \
                 mock.patch.object(accounts, "tmux_new_window", lambda n, c, cmd: "@200"), \
                 mock.patch.object(accounts, "tmux_pipe_pane", lambda w, l: None), \
                 mock.patch.object(accounts, "tmux_send_literal_and_enter", lambda w, t: None), \
                 mock.patch.object(accounts, "wait_for_codex_ready", lambda w, t: True), \
                 mock.patch.object(accounts, "wiki_agent_update", lambda t, w, l: None), \
                 mock.patch.object(accounts, "codex_login_status", lambda: True), \
                 mock.patch.object(accounts, "find_session_id_for_worktree", lambda w: None):
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


if __name__ == "__main__":
    unittest.main()
