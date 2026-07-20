"""Unit tests for backend/app/provider_health.py.

All paths in these tests are temp dirs — no real credentials are touched.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import provider_health


def _write_codex_auth(root: Path, *, refresh: str = "rt_abc") -> Path:
    codex_dir = root / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    auth = codex_dir / "auth.json"
    auth.write_text(json.dumps({"tokens": {"refresh_token": refresh}}), encoding="utf-8")
    return auth


def _write_claude_creds(root: Path, *, body: dict[str, object] | None = None) -> Path:
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    creds = claude_dir / ".credentials.json"
    creds.write_text(json.dumps(body or {"claudeAiOauth": {"accessToken": "x"}}), encoding="utf-8")
    return creds


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "WIKI_ACCOUNT_HOME_OVERRIDE": str(self.root),
                # Point codex probe at the temp dir specifically to avoid any
                # stale WIKI_CODEX_AUTH_PATH from the caller's shell.
                "WIKI_CODEX_AUTH_PATH": str(self.root / ".codex" / "auth.json"),
                "WIKI_CLAUDE_CREDENTIALS_PATH": str(self.root / ".claude" / ".credentials.json"),
                "WIKI_CLAUDE_HOME_PATH": str(self.root / ".claude"),
            },
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_codex_credential_presence_stays_unknown(self) -> None:
        _write_codex_auth(self.root)
        with mock.patch.object(provider_health, "_verify_operationally", return_value=True):
            health = provider_health.probe_codex()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(health.reason_code, "verification_unavailable")
        self.assertIsNotNone(health.checked_at)

    def test_codex_unauthorized_when_refresh_token_missing(self) -> None:
        (self.root / ".codex").mkdir(parents=True, exist_ok=True)
        (self.root / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {}}), encoding="utf-8"
        )
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unauthorized")
        self.assertEqual(health.reason_code, "credentials_invalid")

    def test_codex_unauthorized_when_auth_json_corrupt(self) -> None:
        (self.root / ".codex").mkdir(parents=True, exist_ok=True)
        (self.root / ".codex" / "auth.json").write_text("not-json", encoding="utf-8")
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unauthorized")

    def test_codex_unauthorized_when_auth_json_has_invalid_utf8(self) -> None:
        auth = self.root / ".codex" / "auth.json"
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_bytes(b'{"tokens": {"refresh_token": "\xff"}}')
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unauthorized")
        self.assertEqual(health.reason_code, "credentials_invalid")

    def test_claude_unauthorized_when_credentials_have_invalid_utf8(self) -> None:
        creds = self.root / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_bytes(b'{"claudeAiOauth": {"accessToken": "\xff"}}')
        health = provider_health.probe_claude()
        self.assertEqual(health.status, "unauthorized")
        self.assertEqual(health.reason_code, "credentials_invalid")

    def test_codex_unknown_when_file_missing(self) -> None:
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unknown")

    def test_claude_credential_presence_stays_unknown(self) -> None:
        _write_claude_creds(self.root)
        with mock.patch.object(provider_health, "_verify_operationally", return_value=True):
            health = provider_health.probe_claude()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(health.reason_code, "verification_unavailable")

    def test_claude_unauthorized_when_credentials_empty(self) -> None:
        claude_dir = self.root / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / ".credentials.json").write_text("{}", encoding="utf-8")
        health = provider_health.probe_claude()
        self.assertEqual(health.status, "unauthorized")

    def test_claude_unknown_on_keychain_host(self) -> None:
        # Directory exists (Claude is installed) but no credentials file
        # (macOS Keychain user). Should not raise a false unauthorized alarm.
        (self.root / ".claude").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(provider_health, "_verify_operationally", return_value=None):
            health = provider_health.probe_claude()
        self.assertEqual(health.status, "unknown")

    def test_claude_unknown_when_home_missing(self) -> None:
        with mock.patch.object(provider_health, "_verify_operationally", return_value=None):
            health = provider_health.probe_claude()
        self.assertEqual(health.status, "unknown")

    def test_credential_presence_without_operational_verification_is_unknown(self) -> None:
        _write_codex_auth(self.root)
        with mock.patch.object(provider_health, "_verify_operationally", return_value=None):
            health = provider_health.probe_codex()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(health.reason_code, "verification_unavailable")

    def test_operational_verification_failure_is_coarse_and_secret_free(self) -> None:
        secret = "refresh-token-secret-value"
        _write_codex_auth(self.root, refresh=secret)
        with mock.patch.object(provider_health, "_verify_operationally", return_value=False):
            health = provider_health.probe_codex()
        payload = health.to_dict()
        self.assertEqual(health.status, "unknown")
        self.assertEqual(health.reason_code, "verification_unavailable")
        self.assertNotIn(secret, repr(payload))
        self.assertNotIn(str(self.root), repr(payload))

    def test_status_boundary_decodes_real_child_bytes(self) -> None:
        script = self.root / "status-probe"
        script.write_text("#!/bin/sh\nprintf '%s' '{\"loggedIn\":true}'\n", encoding="utf-8")
        script.chmod(0o755)
        result = provider_health._run_status_command([str(script)], parse_json=True)
        self.assertTrue(result)

    def test_status_boundary_fails_closed_on_malformed_child_bytes(self) -> None:
        script = self.root / "malformed-status-probe"
        script.write_text("#!/bin/sh\nprintf '\\377'\n", encoding="utf-8")
        script.chmod(0o755)
        result = provider_health._run_status_command([str(script)], parse_json=True)
        self.assertIsNone(result)

    def test_local_status_probe_uses_isolated_config_homes(self) -> None:
        _write_codex_auth(self.root)
        seen: dict[str, str] = {}

        def capture(args: list[str], **kwargs: object) -> None:
            env = kwargs["env"]
            assert isinstance(env, dict)
            seen.update({key: str(env[key]) for key in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")})
            return None

        with mock.patch.object(provider_health, "_run_status_command", side_effect=capture):
            provider_health.probe_codex()
        self.assertNotEqual(seen["HOME"], str(Path.home()))
        self.assertNotIn(str(Path.home() / ".codex"), seen["CODEX_HOME"])
        self.assertNotIn(str(Path.home() / ".claude"), seen["CLAUDE_CONFIG_DIR"])


class TrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.codex_auth = _write_codex_auth(self.root)
        self.claude_creds = _write_claude_creds(self.root)

        def codex_path(kind: str) -> Path:
            return self.codex_auth if kind == "cdx" else self.claude_creds

        self.tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=lambda: provider_health.ProviderHealth(
                status="ok", checked_at="t0", reason_code=None
            ),
            probe_claude_fn=lambda: provider_health.ProviderHealth(
                status="ok", checked_at="t0", reason_code=None
            ),
            credential_path_fn=codex_path,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_snapshot_starts_unknown(self) -> None:
        snapshot = self.tracker.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "unknown")
        self.assertEqual(snapshot["cc"]["status"], "unknown")

    def test_refresh_populates_ok(self) -> None:
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "ok")
        self.assertEqual(snapshot["cc"]["status"], "ok")

    def test_mark_unauthorized_survives_probe(self) -> None:
        self.tracker.refresh()
        self.tracker.mark_unauthorized("cdx")
        self.tracker._probe_fns["cdx"] = lambda: provider_health.ProviderHealth(
            status="unauthorized", checked_at="t1", reason_code="verification_failed"
        )
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["reason_code"], "auth_dead")
        # Other providers must be unaffected.
        self.assertEqual(snapshot["cc"]["status"], "ok")

    def test_fake_plausible_credential_cannot_clear_auth_dead(self) -> None:
        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=provider_health.probe_codex,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self.codex_auth,
        )
        with mock.patch.object(provider_health.accounts, "codex_auth_path", return_value=self.codex_auth), mock.patch.object(
            provider_health, "_verify_operationally", return_value=True
        ):
            tracker.refresh(kinds=["cdx"])
            self.assertEqual(tracker.get("cdx").status, "unknown")
            tracker.mark_unauthorized("cdx")
            snapshot = tracker.refresh(kinds=["cdx"])
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["reason_code"], "auth_dead")

    def test_credential_fingerprint_change_alone_does_not_clear_sticky_mark(self) -> None:
        self.tracker.refresh()
        self.tracker.mark_unauthorized("cdx")
        self.tracker._probe_fns["cdx"] = lambda: provider_health.ProviderHealth(
            status="unknown", checked_at="probe", reason_code="verification_unavailable"
        )
        original_mtime = self.codex_auth.stat().st_mtime
        self.codex_auth.write_text(json.dumps({"tokens": {"refresh_token": "new"}}), encoding="utf-8")
        os.utime(self.codex_auth, (original_mtime, original_mtime))
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["reason_code"], "auth_dead")

    def test_authenticated_run_clears_sticky_mark_for_changed_fingerprint(self) -> None:
        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=lambda: provider_health.ProviderHealth(
                status="unknown", checked_at="probe", reason_code="verification_unavailable"
            ),
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self.codex_auth,
        )
        tracker.refresh(kinds=["cdx"])
        tracker.mark_unauthorized("cdx")
        self.codex_auth.write_text(
            json.dumps({"tokens": {"refresh_token": "new"}}), encoding="utf-8"
        )
        tracker.refresh(kinds=["cdx"])
        self.assertEqual(tracker.get("cdx").status, "unauthorized")
        self.assertTrue(
            tracker.mark_authenticated(
                "cdx",
                credential_fingerprint=provider_health._credential_fingerprint(
                    self.codex_auth
                ),
            )
        )
        self.assertEqual(tracker.get("cdx").status, "ok")
        self.assertIsNone(tracker.get("cdx").reason_code)

    def test_explicit_success_clears_sticky_mark_without_file_change(self) -> None:
        self.tracker.refresh()
        self.tracker.mark_unauthorized("cdx")
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "ok")

    def test_refresh_async_keeps_event_loop_responsive_and_cancels_bounded(self) -> None:
        def slow_probe() -> provider_health.ProviderHealth:
            time.sleep(0.15)
            return provider_health.ProviderHealth(status="unknown", checked_at="slow")

        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=slow_probe,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self.codex_auth,
        )

        async def exercise() -> tuple[int, float]:
            ticks = 0
            max_gap = 0.0

            async def ticker() -> None:
                nonlocal ticks, max_gap
                previous = asyncio.get_running_loop().time()
                while True:
                    await asyncio.sleep(0.005)
                    current = asyncio.get_running_loop().time()
                    max_gap = max(max_gap, current - previous)
                    previous = current
                    ticks += 1

            ticker_task = asyncio.create_task(ticker())
            try:
                await asyncio.wait_for(tracker.refresh_async(kinds=["cdx"]), timeout=1)
                self.assertGreaterEqual(ticks, 10)

                cancelled = asyncio.create_task(tracker.refresh_async(kinds=["cdx"]))
                await asyncio.sleep(0.01)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(cancelled, timeout=0.5)

                stop = asyncio.Event()
                loop_task = asyncio.create_task(
                    provider_health.probe_loop(
                        tracker, interval_seconds=60, stop=stop
                    )
                )
                await asyncio.sleep(0.01)
                stop.set()
                await asyncio.wait_for(loop_task, timeout=0.5)
            finally:
                ticker_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await ticker_task
            return ticks, max_gap

        ticks, max_gap = asyncio.run(exercise())
        self.assertGreaterEqual(ticks, 10)
        self.assertLess(max_gap, 0.05)

    def test_checked_at_changes_only_when_a_probe_runs(self) -> None:
        self.tracker.refresh()
        self.assertEqual(self.tracker.get("cdx").checked_at, "t0")
        self.tracker.mark_unauthorized("cdx")
        self.assertEqual(self.tracker.get("cdx").checked_at, "t0")
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["checked_at"], "t0")

    def test_stale_refresh_cannot_overwrite_newer_auth_dead_event(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_probe() -> provider_health.ProviderHealth:
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return provider_health.ProviderHealth(status="ok", checked_at="stale")

        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=blocked_probe,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self.codex_auth,
        )
        refresh_thread = threading.Thread(target=tracker.refresh, kwargs={"kinds": ["cdx"]})
        refresh_thread.start()
        self.assertTrue(entered.wait(timeout=2))
        tracker.mark_unauthorized("cdx")
        release.set()
        refresh_thread.join(timeout=2)
        self.assertFalse(refresh_thread.is_alive())
        self.assertEqual(tracker.get("cdx").status, "unauthorized")
        self.assertEqual(tracker.get("cdx").reason_code, "auth_dead")

    def test_concurrent_refreshes_are_singleflight(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def blocked_probe() -> provider_health.ProviderHealth:
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return provider_health.ProviderHealth(status="unknown", checked_at="t1")

        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=blocked_probe,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self.codex_auth,
        )
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(tracker.refresh, kinds=["cdx"]) for _ in range(12)]
            self.assertTrue(entered.wait(timeout=2))
            release.set()
            snapshots = [future.result(timeout=2) for future in futures]
        self.assertEqual(calls, 1)
        self.assertTrue(all(snapshot["cdx"]["checked_at"] == "t1" for snapshot in snapshots))

    def test_mark_reason_only_fires_for_auth_keywords(self) -> None:
        self.assertFalse(self.tracker.mark_reason("cdx", "adapter_lost"))
        self.assertEqual(self.tracker.get("cdx").status, "unknown")
        self.assertTrue(self.tracker.mark_reason("cdx", "provider reported unauthorized"))
        self.assertEqual(self.tracker.get("cdx").status, "unauthorized")

    def test_mark_reason_matches_token_and_login_words(self) -> None:
        self.assertTrue(
            provider_health.state_reason_indicates_auth("access token could not be refreshed")
        )
        self.assertTrue(provider_health.state_reason_indicates_auth("please run codex login"))
        self.assertFalse(provider_health.state_reason_indicates_auth("worker was killed by user"))
        self.assertFalse(provider_health.state_reason_indicates_auth(None))

    def test_spawn_hint_only_when_unauthorized(self) -> None:
        self.tracker.refresh()
        self.assertIsNone(self.tracker.spawn_hint("cdx"))
        self.tracker.mark_unauthorized("cdx")
        hint = self.tracker.spawn_hint("cdx")
        self.assertIsNotNone(hint)
        self.assertIn("codex login", hint or "")

    def test_unknown_kind_is_noop(self) -> None:
        self.tracker.mark_unauthorized("bogus")
        self.assertNotIn("bogus", self.tracker.snapshot())


class MainWiringTests(unittest.TestCase):
    """Verify /api/providers/health endpoint and event bridge integration."""

    def setUp(self) -> None:
        from backend.app import main

        self._main = main
        self._prior = main.PROVIDER_HEALTH
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self._codex_auth = _write_codex_auth(root)
        self._claude_creds = _write_claude_creds(root)
        self.probe_calls = 0

        def credential_path(kind: str) -> Path:
            return self._codex_auth if kind == "cdx" else self._claude_creds

        def probe() -> provider_health.ProviderHealth:
            self.probe_calls += 1
            return provider_health.ProviderHealth(
                status="ok", checked_at="t0", reason_code=None
            )

        main.PROVIDER_HEALTH = provider_health.ProviderHealthTracker(
            probe_codex_fn=probe,
            probe_claude_fn=probe,
            credential_path_fn=credential_path,
        )

    def tearDown(self) -> None:
        self._main.PROVIDER_HEALTH = self._prior
        self._tmp.cleanup()

    def test_endpoint_returns_snapshot(self) -> None:
        result = self._main.get_provider_health()
        self.assertIn("cdx", result)
        self.assertIn("cc", result)
        self.assertIn("status", result["cdx"])

    def test_endpoint_refresh_triggers_probe(self) -> None:
        result = self._main.get_provider_health(refresh=True)
        self.assertEqual(result["cdx"]["status"], "ok")
        self.assertEqual(result["cc"]["status"], "ok")

    def test_sequential_refresh_storm_is_cooled_down(self) -> None:
        self._main.get_provider_health(refresh=True)
        calls_after_first_refresh = self.probe_calls
        for _ in range(10):
            self._main.get_provider_health(refresh=True)
        self.assertEqual(self.probe_calls, calls_after_first_refresh)

    def test_refresh_handles_invalid_utf8_credentials(self) -> None:
        self._codex_auth.write_bytes(b'{"tokens": {"refresh_token": "\xff"}}')
        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=provider_health.probe_codex,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self._codex_auth,
        )
        self._main.PROVIDER_HEALTH = tracker
        with mock.patch.object(
            self._main.accounts, "codex_auth_path", return_value=self._codex_auth
        ):
            result = self._main.get_provider_health(refresh=True)
        self.assertEqual(result["cdx"]["status"], "unauthorized")
        self.assertEqual(result["cdx"]["reason_code"], "credentials_invalid")

    def test_lifespan_primes_with_invalid_utf8_credentials(self) -> None:
        self._codex_auth.write_bytes(b'{"tokens": {"refresh_token": "\xff"}}')
        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=provider_health.probe_codex,
            probe_claude_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            credential_path_fn=lambda kind: self._codex_auth,
        )
        self._main.PROVIDER_HEALTH = tracker

        async def wait_forever(*args: object, **kwargs: object) -> None:
            await asyncio.Future()

        async def fake_dispatcher() -> tuple[asyncio.Task, asyncio.Task, asyncio.Task]:
            return tuple(
                asyncio.create_task(wait_forever()) for _ in range(3)
            )  # type: ignore[return-value]

        async def exercise() -> None:
            async with self._main.lifespan(self._main.app):
                self.assertEqual(
                    self._main.PROVIDER_HEALTH.get("cdx").reason_code,
                    "credentials_invalid",
                )

        with (
            mock.patch.object(
                self._main.accounts, "codex_auth_path", return_value=self._codex_auth
            ),
            mock.patch.object(self._main, "_start_dispatcher", side_effect=fake_dispatcher),
            mock.patch.object(self._main.knowledge, "background_index_loop", side_effect=wait_forever),
            mock.patch.object(self._main.provider_health, "probe_loop", side_effect=wait_forever),
            mock.patch.object(self._main.terminal.TERMINAL_MANAGER, "close_all"),
        ):
            asyncio.run(exercise())

    def test_lifespan_survives_malformed_probe_command_output(self) -> None:
        bin_dir = Path(self._tmp.name) / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\nprintf '\\377'\n", encoding="utf-8")
        claude.chmod(0o755)
        claude_creds = self._claude_creds
        tracker = provider_health.ProviderHealthTracker(
            probe_codex_fn=lambda: provider_health.ProviderHealth(status="unknown"),
            probe_claude_fn=provider_health.probe_claude,
            credential_path_fn=lambda kind: self._codex_auth
            if kind == "cdx"
            else claude_creds,
        )
        self._main.PROVIDER_HEALTH = tracker

        async def wait_forever(*args: object, **kwargs: object) -> None:
            await asyncio.Future()

        async def fake_dispatcher() -> tuple[asyncio.Task, asyncio.Task, asyncio.Task]:
            return tuple(
                asyncio.create_task(wait_forever()) for _ in range(3)
            )  # type: ignore[return-value]

        async def exercise() -> None:
            async with self._main.lifespan(self._main.app):
                self.assertEqual(
                    self._main.PROVIDER_HEALTH.get("cc").reason_code,
                    "verification_unavailable",
                )

        with (
            mock.patch.dict(
                os.environ,
                {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            ),
            mock.patch.object(
                self._main.provider_health,
                "claude_credentials_path",
                return_value=claude_creds,
            ),
            mock.patch.object(self._main, "_start_dispatcher", side_effect=fake_dispatcher),
            mock.patch.object(self._main.knowledge, "background_index_loop", side_effect=wait_forever),
            mock.patch.object(self._main.provider_health, "probe_loop", side_effect=wait_forever),
            mock.patch.object(self._main.terminal.TERMINAL_MANAGER, "close_all"),
        ):
            asyncio.run(exercise())

    def test_lifespan_ignores_unexpected_prime_probe_exception(self) -> None:
        tracker = provider_health.ProviderHealthTracker()
        self._main.PROVIDER_HEALTH = tracker

        async def wait_forever(*args: object, **kwargs: object) -> None:
            await asyncio.Future()

        async def fake_dispatcher() -> tuple[asyncio.Task, asyncio.Task, asyncio.Task]:
            return tuple(
                asyncio.create_task(wait_forever()) for _ in range(3)
            )  # type: ignore[return-value]

        async def exercise() -> None:
            async with self._main.lifespan(self._main.app):
                self.assertIsNotNone(self._main.PROVIDER_HEALTH)

        with (
            mock.patch.object(
                tracker,
                "refresh_async",
                side_effect=RuntimeError("fixture probe failure"),
            ),
            mock.patch.object(self._main, "_start_dispatcher", side_effect=fake_dispatcher),
            mock.patch.object(self._main.knowledge, "background_index_loop", side_effect=wait_forever),
            mock.patch.object(self._main.provider_health, "probe_loop", side_effect=wait_forever),
            mock.patch.object(self._main.terminal.TERMINAL_MANAGER, "close_all"),
        ):
            asyncio.run(exercise())

    def test_successful_authenticated_event_clears_changed_credential(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main.PROVIDER_HEALTH.mark_unauthorized("cdx")
        original_mtime = self._codex_auth.stat().st_mtime
        self._codex_auth.write_text(
            json.dumps({"tokens": {"refresh_token": "new"}}), encoding="utf-8"
        )
        os.utime(self._codex_auth, (original_mtime, original_mtime))
        fingerprint = provider_health._credential_fingerprint(self._codex_auth)
        asyncio.run(
            self._main.publish_agent_event(
                {
                    "type": "codex_auth_verified",
                    "provider": "codex",
                    "credential_source": "current",
                    "success": True,
                    "credential_fingerprint": fingerprint,
                }
            )
        )
        self.assertEqual(self._main.PROVIDER_HEALTH.get("cdx").status, "ok")

    def test_old_turn_fingerprint_cannot_clear_new_credential(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main.PROVIDER_HEALTH.mark_unauthorized("cdx")
        old_fingerprint = provider_health._credential_fingerprint(self._codex_auth)
        self._codex_auth.write_text(
            json.dumps({"tokens": {"refresh_token": "new"}}), encoding="utf-8"
        )
        asyncio.run(
            self._main.publish_agent_event(
                {
                    "type": "codex_auth_verified",
                    "provider": "codex",
                    "credential_source": "current",
                    "success": True,
                    "credential_fingerprint": old_fingerprint,
                }
            )
        )
        self.assertEqual(self._main.PROVIDER_HEALTH.get("cdx").status, "unauthorized")
        self.assertEqual(
            self._main.PROVIDER_HEALTH.get("cdx").reason_code, "auth_dead"
        )

    def test_codex_auth_dead_event_marks_cdx_unauthorized(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main._consume_provider_health_signal(
            {
                "type": "codex_auth_dead_exhausted",
                "provider": "codex",
                "failure": "auth",
                "credential_source": "current",
                "exhausted": True,
                "tickets": ["WIKI-99"],
            }
        )
        snapshot = self._main.PROVIDER_HEALTH.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["reason_code"], "auth_dead")
        # cc must not be flipped by codex-scoped events.
        self.assertEqual(snapshot["cc"]["status"], "ok")

    def test_successful_or_failed_revival_does_not_mark_provider_dead(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main._consume_provider_health_signal(
            {
                "type": "codex_auth_dead_revival",
                "revived": [],
                "failed": ["WIKI-99"],
                "failed_reasons": {"WIKI-99": "token could not be refreshed"},
            }
        )
        snapshot = self._main.PROVIDER_HEALTH.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "ok")

    def test_non_auth_or_non_current_exhaustion_does_not_mark_provider_dead(self) -> None:
        self._main.get_provider_health(refresh=True)
        for event in (
            {
                "type": "codex_auth_dead_exhausted",
                "provider": "codex",
                "failure": "runtime",
                "credential_source": "current",
                "exhausted": True,
            },
            {
                "type": "codex_auth_dead_exhausted",
                "provider": "codex",
                "failure": "auth",
                "credential_source": "rotated",
                "exhausted": True,
            },
        ):
            self._main._consume_provider_health_signal(event)
        self.assertEqual(self._main.PROVIDER_HEALTH.get("cdx").status, "ok")

    def test_unrelated_events_leave_health_alone(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main._consume_provider_health_signal(
            {"type": "session", "ticket": "WIKI-99"}
        )
        snapshot = self._main.PROVIDER_HEALTH.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "ok")

    def test_publish_agent_event_consumes_legacy_watchdog_health_signal(self) -> None:
        self._main.get_provider_health(refresh=True)
        asyncio.run(
            self._main.publish_agent_event(
                {
                    "type": "codex_auth_dead_exhausted",
                    "provider": "codex",
                    "failure": "auth",
                    "credential_source": "current",
                    "exhausted": True,
                    "tickets": ["WIKI-99"],
                }
            )
        )
        self.assertEqual(self._main.PROVIDER_HEALTH.get("cdx").status, "unauthorized")


if __name__ == "__main__":
    unittest.main()
