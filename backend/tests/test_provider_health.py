"""Unit tests for backend/app/provider_health.py.

All paths in these tests are temp dirs — no real credentials are touched.
"""

from __future__ import annotations

import json
import os
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

    def test_codex_ok_when_refresh_token_present(self) -> None:
        _write_codex_auth(self.root)
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "ok")
        self.assertIsNone(health.detail)
        self.assertIsNotNone(health.checked_at)

    def test_codex_unauthorized_when_refresh_token_missing(self) -> None:
        (self.root / ".codex").mkdir(parents=True, exist_ok=True)
        (self.root / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {}}), encoding="utf-8"
        )
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unauthorized")
        self.assertIn("refresh_token", (health.detail or ""))

    def test_codex_unauthorized_when_auth_json_corrupt(self) -> None:
        (self.root / ".codex").mkdir(parents=True, exist_ok=True)
        (self.root / ".codex" / "auth.json").write_text("not-json", encoding="utf-8")
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unauthorized")

    def test_codex_unknown_when_file_missing(self) -> None:
        health = provider_health.probe_codex()
        self.assertEqual(health.status, "unknown")

    def test_claude_ok_when_credentials_present(self) -> None:
        _write_claude_creds(self.root)
        health = provider_health.probe_claude()
        self.assertEqual(health.status, "ok")

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
        health = provider_health.probe_claude()
        self.assertEqual(health.status, "unknown")

    def test_claude_unknown_when_home_missing(self) -> None:
        health = provider_health.probe_claude()
        self.assertEqual(health.status, "unknown")


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
                status="ok", checked_at="t0", detail=None
            ),
            probe_claude_fn=lambda: provider_health.ProviderHealth(
                status="ok", checked_at="t0", detail=None
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
        self.tracker.mark_unauthorized("cdx", detail="refresh failed")
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["detail"], "refresh failed")
        # Other providers must be unaffected.
        self.assertEqual(snapshot["cc"]["status"], "ok")

    def test_credential_file_advance_clears_sticky_mark(self) -> None:
        self.tracker.refresh()
        self.tracker.mark_unauthorized("cdx", detail="refresh failed")
        # Bump mtime so the sticky mark expires on next refresh.
        past = time.time() + 60
        os.utime(self.codex_auth, (past, past))
        snapshot = self.tracker.refresh()
        self.assertEqual(snapshot["cdx"]["status"], "ok")

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
        self.tracker.mark_unauthorized("cdx", detail="refresh failed")
        hint = self.tracker.spawn_hint("cdx")
        self.assertIsNotNone(hint)
        self.assertIn("codex login", hint or "")

    def test_unknown_kind_is_noop(self) -> None:
        self.tracker.mark_unauthorized("bogus", detail="ignored")
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

        def credential_path(kind: str) -> Path:
            return self._codex_auth if kind == "cdx" else self._claude_creds

        main.PROVIDER_HEALTH = provider_health.ProviderHealthTracker(
            probe_codex_fn=lambda: provider_health.ProviderHealth(
                status="ok", checked_at="t0", detail=None
            ),
            probe_claude_fn=lambda: provider_health.ProviderHealth(
                status="ok", checked_at="t0", detail=None
            ),
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

    def test_codex_auth_dead_event_marks_cdx_unauthorized(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main._consume_provider_health_signal(
            {
                "type": "codex_auth_dead_exhausted",
                "tickets": ["WIKI-99"],
            }
        )
        snapshot = self._main.PROVIDER_HEALTH.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertIn("exhausted", (snapshot["cdx"]["detail"] or "").lower())
        # cc must not be flipped by codex-scoped events.
        self.assertEqual(snapshot["cc"]["status"], "ok")

    def test_codex_auth_dead_revival_uses_failed_reason_detail(self) -> None:
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
        self.assertEqual(snapshot["cdx"]["status"], "unauthorized")
        self.assertEqual(snapshot["cdx"]["detail"], "token could not be refreshed")

    def test_unrelated_events_leave_health_alone(self) -> None:
        self._main.get_provider_health(refresh=True)
        self._main._consume_provider_health_signal(
            {"type": "session", "ticket": "WIKI-99"}
        )
        snapshot = self._main.PROVIDER_HEALTH.snapshot()
        self.assertEqual(snapshot["cdx"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
