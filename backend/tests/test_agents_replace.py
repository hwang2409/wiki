"""Regression tests for the /agents Replace protocol.

All registry, status, prompt, and log paths are temp dirs. Tmux is mocked and
only fake window ids are used.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi import HTTPException

from backend.app import agent_replace


class AgentReplaceTests(unittest.TestCase):
    def _patch_paths(self, root: Path):
        status_dir = root / "status"
        tmp_dir = root / "tmp"
        archive_dir = root / "archive"
        status_dir.mkdir()
        tmp_dir.mkdir()
        archive_dir.mkdir()
        registry = root / "agent-registry.json"
        return mock.patch.multiple(
            agent_replace,
            AGENT_REGISTRY_PATH=registry,
            AGENT_STATUS_DIR=status_dir,
            AGENT_TMP_DIR=tmp_dir,
            AGENT_ARCHIVE_DIR=archive_dir,
        )

    def test_replace_worker_kills_spawns_and_reregisters_handoff(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            prior_transcript = root / "prior.jsonl"
            prior_transcript.write_text("{}\n", encoding="utf-8")
            old_log = root / "tmp" / "cdx-WIKI-38-r2.log"
            registry = {
                "WIKI-38": {
                    "current": {
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "model": "gpt-5.5",
                        "worktree": str(worktree),
                        "log": str(old_log),
                        "orch": "wiki",
                        "session": 3,
                        "session_id": "old-session-123",
                        "spawned_at": "2026-07-09T10:00:00+00:00",
                    },
                    "history": [],
                },
                "_orchestrators": {"wiki": {"window": "@9", "cwd": str(root), "model": "opus"}},
            }
            operations: list[tuple] = []

            def fake_new(name: str, cwd: Path, command: str, target_session: str) -> str:
                operations.append(("new", name, str(cwd), command, target_session))
                return "@100"

            with self._patch_paths(root):
                agent_replace.AGENT_REGISTRY_PATH.write_text(json.dumps(registry), encoding="utf-8")
                (agent_replace.AGENT_STATUS_DIR / "WIKI-38.json").write_text(
                    json.dumps({
                        "state": "working",
                        "pr": "https://github.com/hwang2409/wiki/pull/38",
                        "step": "halfway",
                        "blocker": None,
                    }),
                    encoding="utf-8",
                )
                with mock.patch.object(agent_replace, "tmux_live_windows", lambda: {"@42"}), \
                     mock.patch.object(agent_replace, "_tmux_window_session", lambda window: operations.append(("session", window)) or "scratch"), \
                     mock.patch.object(agent_replace, "_tmux_keepalive_if_last", lambda session: None), \
                     mock.patch.object(agent_replace, "_tmux_kill_window", lambda window: operations.append(("kill", window))), \
                     mock.patch.object(agent_replace, "_tmux_new_window", fake_new), \
                     mock.patch.object(agent_replace, "_tmux_lock_window_name", lambda window: operations.append(("lock", window))), \
                     mock.patch.object(agent_replace, "_tmux_pipe_pane", lambda window, log: operations.append(("pipe", window, str(log)))), \
                     mock.patch.object(agent_replace, "_worker_transcript_path", lambda ticket, current: str(prior_transcript)):
                    result = agent_replace.replace_agent("WIKI-38")

                self.assertEqual(result["type"], "worker")
                self.assertEqual(result["window"], "@100")
                self.assertEqual(operations[0], ("session", "@42"))
                self.assertEqual(operations[1], ("kill", "@42"))
                new_op = operations[2]
                self.assertEqual(new_op[0], "new")
                self.assertEqual(new_op[1], "cdx:WIKI-38")
                self.assertEqual(new_op[2], str(worktree.resolve()))
                self.assertEqual(new_op[4], "scratch")
                self.assertIn("codex --yolo -m gpt-5.5", new_op[3])
                self.assertIn("model_reasoning_effort=high", new_op[3])

                reloaded = json.loads(agent_replace.AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
                current = reloaded["WIKI-38"]["current"]
                self.assertEqual(current["window"], "@100")
                self.assertEqual(current["kind"], "cdx")
                self.assertEqual(current["model"], "gpt-5.5")
                self.assertEqual(current["orch"], "wiki")
                self.assertEqual(len(reloaded["WIKI-38"]["history"]), 1)
                self.assertEqual(reloaded["WIKI-38"]["history"][0]["outcome"], "handoff")
                self.assertEqual(reloaded["WIKI-38"]["history"][0]["session_id"], "old-session-123")

                prompt_path = Path(result["prompt_path"])
                prompt = prompt_path.read_text(encoding="utf-8")
                self.assertIn("ticket WIKI-38", prompt)
                self.assertIn(
                    "You are a replacement WORKER, replacing agent run/session `old-session-123`",
                    prompt,
                )
                self.assertIn(str(old_log), prompt)
                self.assertIn(str(prior_transcript), prompt)
                self.assertIn(str(agent_replace.AGENT_STATUS_DIR / "WIKI-38.json"), prompt)
                self.assertIn("latest PR handoff comment", prompt)
                self.assertTrue(str(result["log"]).endswith("cdx-WIKI-38-r3.log"))

    def test_replace_orchestrator_targets_original_session_and_prompts_self_register(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            registry = {
                "_orchestrators": {
                    "wiki": {
                        "window": "@9",
                        "cwd": str(cwd),
                        "model": "sonnet",
                        "session_id": "orch-session-1",
                        "transcript": str(root / "old-orch.jsonl"),
                    }
                }
            }
            operations: list[tuple] = []

            def fake_new(name: str, cwd_arg: Path, command: str, target_session: str) -> str:
                operations.append(("new", name, str(cwd_arg), command, target_session))
                return "@101"

            with self._patch_paths(root):
                agent_replace.AGENT_REGISTRY_PATH.write_text(json.dumps(registry), encoding="utf-8")
                with mock.patch.object(agent_replace, "tmux_live_windows", lambda: {"@9"}), \
                     mock.patch.object(agent_replace, "_tmux_window_session", lambda window: operations.append(("session", window)) or "wiki-session"), \
                     mock.patch.object(agent_replace, "_tmux_keepalive_if_last", lambda session: None), \
                     mock.patch.object(agent_replace, "_tmux_kill_window", lambda window: operations.append(("kill", window))), \
                     mock.patch.object(agent_replace, "_tmux_new_window", fake_new), \
                     mock.patch.object(agent_replace, "_tmux_lock_window_name", lambda window: operations.append(("lock", window))), \
                     mock.patch.object(agent_replace, "_tmux_pipe_pane", lambda window, log: operations.append(("pipe", window, str(log)))), \
                     mock.patch.object(agent_replace, "settle_claude_kickoff", lambda window, prompt: operations.append(("settle", window))):
                    result = agent_replace.replace_agent("wiki")

                self.assertEqual(result["type"], "orchestrator")
                self.assertEqual(result["window"], "@101")
                self.assertEqual(result["model"], "sonnet")
                self.assertEqual(operations[0], ("session", "@9"))
                self.assertEqual(operations[1], ("kill", "@9"))
                new_op = operations[2]
                self.assertEqual(new_op[1], "thinker")
                self.assertEqual(new_op[2], str(cwd.resolve()))
                self.assertEqual(new_op[4], "wiki-session")
                self.assertIn("claude --model sonnet", new_op[3])

                prompt = Path(result["prompt_path"]).read_text(encoding="utf-8")
                self.assertIn("replacement ORCHESTRATOR with id `wiki`", prompt)
                self.assertIn("replacing session `orch-session-1`", prompt)
                self.assertIn("wiki agent orch wiki --model sonnet", prompt)
                self.assertIn("vault/tools/orchestrator-worker-protocol.md", prompt)

    def test_replace_dead_window_returns_conflict_without_kill(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            registry = {
                "WIKI-38": {
                    "current": {
                        "window": "@42",
                        "kind": "cc",
                        "role": "implement",
                        "model": "sonnet",
                        "worktree": str(worktree),
                        "log": str(root / "tmp" / "cc-WIKI-38.log"),
                    }
                }
            }
            killed: list[str] = []

            with self._patch_paths(root):
                agent_replace.AGENT_REGISTRY_PATH.write_text(json.dumps(registry), encoding="utf-8")
                with mock.patch.object(agent_replace, "tmux_live_windows", lambda: set()), \
                     mock.patch.object(agent_replace, "_tmux_kill_window", lambda window: killed.append(window)):
                    with self.assertRaises(HTTPException) as raised:
                        agent_replace.replace_agent("WIKI-38")

                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(killed, [])


if __name__ == "__main__":
    unittest.main()
