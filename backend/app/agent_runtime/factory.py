from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from .claude import ClaudeStreamAdapter
from .codex import CodexAppServerAdapter
from .provider import ProviderAdapter, ProviderProcessError
from .types import ProviderKind, RunRecord
from .wk_feature import wk_enabled


class RealAdapterFactory:
    """Construct provider adapters with durable run context already bound."""

    def __init__(
        self,
        *,
        codex_command: Sequence[str] = ("codex", "app-server", "--stdio"),
        claude_command: Sequence[str] = ("claude",),
        env: Mapping[str, str] | None = None,
    ):
        self.codex_command = tuple(codex_command)
        self.claude_command = tuple(claude_command)
        self.env = dict(env) if env is not None else None

    def __call__(self, record: RunRecord) -> ProviderAdapter:
        if record.execution_kind in {"wk-claude", "wk-codex"}:
            if not wk_enabled():
                raise ProviderProcessError("wk execution kind is disabled")
            from .wk_adapter import WkProviderAdapter

            status_dir = Path(
                (self.env or {}).get(
                    "WIKI_AGENT_STATUS_DIR",
                    os.environ.get("WIKI_AGENT_STATUS_DIR", "/tmp/agent-status"),
                )
            )
            command = (
                self.codex_command
                if record.execution_kind == "wk-codex"
                else self.claude_command
            )
            return WkProviderAdapter(
                record,
                status_path=status_dir / f"{record.agent_id}.json",
                command=command,
                env=self.env,
            )
        if record.provider is ProviderKind.CODEX:
            return CodexAppServerAdapter(
                record,
                command=self.codex_command,
                env=self.env,
            )
        return ClaudeStreamAdapter(
            record,
            command=self.claude_command,
            env=self.env,
        )
