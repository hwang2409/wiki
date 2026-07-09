"""Focused tests for the WIKI-19 transcript resolver additions.

Only covers the pieces that changed: registry session_id preference and the
same-cwd chain rule after `codex resume`. Other transcripts.py behavior is
covered by manual QA on live rollouts.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import transcripts


def _write_rollout(day_dir: Path, name: str, cwd: str, session_id: str,
                   kickoff_ticket: str | None = None, mtime: float | None = None) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{name}.jsonl"
    lines = [json.dumps({"payload": {"cwd": cwd, "id": session_id}})]
    if kickoff_ticket:
        lines.append(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"You are worker for ticket {kickoff_ticket}. Go.",
            },
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_claude_rollout(project_dir: Path, session_id: str, *,
                          cwd: str, kickoff_ticket: str | None = None,
                          mtime: float | None = None) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    lines = [json.dumps({"payload": {"cwd": cwd, "id": session_id}})]
    if kickoff_ticket:
        lines.append(json.dumps({
            "type": "user",
            "timestamp": "2026-07-09T01:00:00Z",
            "message": {"content": f"You are a worker for Linear ticket {kickoff_ticket}. Go."},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class RegistryIdBeatsDiscoveryTests(unittest.TestCase):
    def test_registry_id_confines_result_to_anchor_cwd(self) -> None:
        """Discovery mode has a stale-worker foot-gun: a NEWER rollout in a
        DIFFERENT cwd (a sibling ticket, wrong resume, etc.) that also matches
        the kickoff regex will win. Registry-id mode must anchor to the pinned
        rollout's cwd — the sibling in another cwd MUST NOT be returned."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            worktree = Path(tmp) / "wiki-15"
            other_cwd = Path(tmp) / "wiki-15-copy"
            # NEWER kickoff-matched rollout in a DIFFERENT cwd (would win under
            # discovery-only rules).
            _write_rollout(day_dir, "decoy", cwd=str(other_cwd),
                           session_id="sess-decoy",
                           kickoff_ticket="WIKI-15", mtime=time.time())
            # Anchor pinned by registry, older, worktree cwd.
            _write_rollout(day_dir, "pinned", cwd=str(worktree),
                           session_id="sess-pinned",
                           kickoff_ticket="WIKI-15", mtime=time.time() - 3600)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                without_id = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id=None
                )
                with_id = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-pinned"
                )
            self.assertIsNotNone(without_id)
            self.assertIsNotNone(with_id)
            # Discovery picks the decoy — that's the whole point of the id path.
            self.assertEqual(without_id.name, "rollout-decoy.jsonl")
            # Registry-id path stays confined to anchor cwd.
            self.assertEqual(with_id.name, "rollout-pinned.jsonl")

    def test_registry_id_returns_anchor_when_no_newer_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            worktree = Path(tmp) / "wiki-15"
            # Only one rollout in cwd — the pinned one.
            _write_rollout(day_dir, "pinned", cwd=str(worktree),
                           session_id="sess-pinned",
                           kickoff_ticket="WIKI-15", mtime=time.time())
            # An unrelated rollout in a different cwd should NOT be chained in.
            _write_rollout(day_dir, "other", cwd=str(Path(tmp) / "other-wt"),
                           session_id="sess-other",
                           kickoff_ticket="WIKI-99", mtime=time.time() + 10)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                found = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-pinned"
                )
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "rollout-pinned.jsonl")


class ClaudeSessionIdResolverTests(unittest.TestCase):
    def test_session_id_primary_path_wins_without_slug_match(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            session_id = "599b561a-7d40-4492-ab82-35a3ae91f733"
            now = datetime.now(tz=timezone.utc)
            project_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475-eval-rerun"
            expected = _write_claude_rollout(
                project_dir,
                session_id,
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                found = transcripts.find_session(
                    "cc", "PR-10475", now.isoformat(), session_id=session_id
                )
            self.assertIsNotNone(found)
            self.assertEqual(found, ("claude", expected))

    def test_slug_glob_fallback_remains_when_session_id_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            now = datetime.now(tz=timezone.utc)
            project_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475-eval-rerun"
            expected = _write_claude_rollout(
                project_dir,
                "fallback-session",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                kickoff_ticket="PR-10475",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                found = transcripts.find_session("cc", "PR-10475", now.isoformat())
            self.assertIsNotNone(found)
            self.assertEqual(found, ("claude", expected))

    def test_session_id_beats_newer_slug_match(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".claude" / "projects"
            now = datetime.now(tz=timezone.utc)
            pinned_dir = root / "eval-rerun"
            slug_dir = root / "-Users-henry-me-fun-phoebe--claude-worktrees-pr10475"
            pinned = _write_claude_rollout(
                pinned_dir,
                "599b561a-7d40-4492-ab82-35a3ae91f733",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475-eval-rerun",
                mtime=time.time() - 3600,
            )
            slug = _write_claude_rollout(
                slug_dir,
                "slug-session",
                cwd="/Users/henry/me/fun/phoebe/claude-worktrees/pr10475",
                kickoff_ticket="PR-10475",
                mtime=time.time(),
            )
            with mock.patch.object(transcripts, "CLAUDE_PROJECTS_DIR", root):
                pinned_found = transcripts.find_session(
                    "cc", "PR-10475", now.isoformat(), session_id="599b561a-7d40-4492-ab82-35a3ae91f733"
                )
                slug_found = transcripts.find_session("cc", "PR-10475", now.isoformat())
            self.assertEqual(pinned_found, ("claude", pinned))
            self.assertEqual(slug_found, ("claude", slug))


class SameCwdChainRuleTests(unittest.TestCase):
    """`codex resume <id>` REUSES the anchor rollout file (verified live
    2026-07-08 via open file handles), so the exact-id file IS the live
    session. The same-cwd chain caused cross-ticket bleed for sessions
    sharing a cwd and now applies ONLY when the anchor file vanished."""

    def test_exact_anchor_wins_over_newer_same_cwd_sibling(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            shared_cwd = Path(tmp) / "repo-root"
            older = time.time() - 3600
            newer = time.time()
            _write_rollout(day_dir, "anchor", cwd=str(shared_cwd),
                           session_id="sess-anchor",
                           kickoff_ticket="WIKI-15", mtime=older)
            # A DIFFERENT ticket's newer session in the same cwd must not win.
            _write_rollout(day_dir, "other-ticket", cwd=str(shared_cwd),
                           session_id="sess-other", mtime=newer)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                found = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-anchor"
                )
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "rollout-anchor.jsonl")


if __name__ == "__main__":
    unittest.main()
