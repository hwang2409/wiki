from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from backend.app import transcripts
from backend.app.agent_runtime.factory import RealAdapterFactory
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import LifecycleState, ProviderKind, RunRecord


FIXTURES = Path(__file__).parent / "fixtures" / "agent_runtime"
PROMPT = (
    "Call render_artifact with kind mermaid and payload source "
    '"graph TD; A-->B", then stop.'
)


class WikiArtifactsCallabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.paths = RuntimePaths(
            runtime_dir=self.root / "runtime",
            socket_path=self.root / "runtime" / "supervisor.sock",
            registry_path=self.root / "agent-registry.json",
            archive_dir=self.root / "archive",
            status_dir=self.root / "status",
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.root / "home"),
                "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                "FAKE_ARTIFACT_TOOL": "1",
                "FAKE_CODEX_TRANSCRIPT_DIR": str(self.root / "codex-sessions"),
                "FAKE_PROTOCOL_LOG": str(self.root / "provider-protocol.jsonl"),
                "TMUX": "must-not-leak",
                "TMUX_PANE": "%9999",
                "WIKI_AGENT_REGISTRY_PATH": str(self.paths.registry_path),
                "WIKI_AGENT_RUNTIME_DIR": str(self.paths.runtime_dir),
            }
        )
        factory = RealAdapterFactory(
            codex_command=(
                sys.executable,
                "-u",
                str(FIXTURES / "fake_codex_app_server.py"),
            ),
            claude_command=(
                sys.executable,
                "-u",
                str(FIXTURES / "fake_claude_stream.py"),
            ),
            env=env,
        )
        self.store = RunStore(self.paths)
        self.supervisor = Supervisor(self.store, factory)

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        self.tmp.cleanup()

    async def _wait_for_artifact_frame(self, record: RunRecord) -> list[dict]:
        for _ in range(300):
            raw = self.store.read_raw_events(record.run_id)
            if any(self._artifact_tool_name(row) == "render_artifact" for row in raw):
                return raw
            await asyncio.sleep(0.01)
        self.fail(f"{record.provider.value} run never emitted render_artifact")

    @staticmethod
    def _artifact_tool_name(row: dict) -> str | None:
        payload = row.get("payload") or {}
        item = (payload.get("params") or {}).get("item") or {}
        if item.get("type") == "mcpToolCall":
            return item.get("tool")
        if payload.get("type") != "assistant":
            return None
        for block in (payload.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    return name.rsplit("__", 1)[-1]
        return None

    async def test_each_real_adapter_path_emits_a_render_artifact_tool_frame(
        self,
    ) -> None:
        for provider in (ProviderKind.CODEX, ProviderKind.CLAUDE):
            with self.subTest(provider=provider.value):
                record = await self.supervisor.start_run(
                    agent_id=f"WIKI-89-{provider.value.upper()}",
                    provider=provider,
                    role="implement",
                    model=f"fixture-{provider.value}",
                    effort="low" if provider is ProviderKind.CODEX else None,
                    worktree=str(self.worktree),
                    prompt=PROMPT,
                )
                raw = await self._wait_for_artifact_frame(record)
                self.assertTrue(
                    any(self._artifact_tool_name(row) == "render_artifact" for row in raw)
                )

                current = self.store.get(record.run_id)
                self.assertIn(
                    current.state,
                    {LifecycleState.WORKING, LifecycleState.IDLE},
                )
                transcript_path = Path(str(current.transcript_path))
                transcript_rows = [
                    json.loads(line)
                    for line in transcript_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(
                    any("render_artifact" in json.dumps(row) for row in transcript_rows)
                )
                session = transcripts.read_session_delta(
                    provider.value,
                    transcript_path,
                )
                artifact_events = [
                    event for event in session["events"] if event.get("kind") == "artifact"
                ]
                self.assertEqual(len(artifact_events), 1)
                self.assertEqual(
                    artifact_events[0]["artifact"],
                    {"kind": "mermaid", "source": "graph TD; A-->B"},
                )
                self.assertFalse(
                    any(
                        event.get("kind") == "tool"
                        and (event.get("tool") or {}).get("summary")
                        == "render_artifact rejected"
                        for event in session["events"]
                    )
                )


if __name__ == "__main__":
    unittest.main()
