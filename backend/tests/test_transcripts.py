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


class SameCwdChainRuleTests(unittest.TestCase):
    """After `codex resume`, a NEW rollout is written with a NEW id in the
    SAME cwd. The resolver, once anchored by the (older) registry id, must
    prefer the newer sibling in the same cwd."""

    def test_newer_same_cwd_rollout_wins_over_anchor(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            now = datetime.now(tz=timezone.utc)
            day_dir = root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            worktree = Path(tmp) / "wiki-15"
            older = time.time() - 3600
            newer = time.time()
            _write_rollout(day_dir, "anchor", cwd=str(worktree),
                           session_id="sess-anchor",
                           kickoff_ticket="WIKI-15", mtime=older)
            _write_rollout(day_dir, "post-resume", cwd=str(worktree),
                           session_id="sess-post-resume", mtime=newer)
            with mock.patch.object(transcripts, "CODEX_SESSIONS_DIR", root):
                found = transcripts.find_codex_session(
                    "WIKI-15", now.isoformat(), session_id="sess-anchor"
                )
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "rollout-post-resume.jsonl")


if __name__ == "__main__":
    unittest.main()
