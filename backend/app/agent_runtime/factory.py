from __future__ import annotations

from collections.abc import Mapping, Sequence

from .claude import ClaudeStreamAdapter
from .codex import CodexAppServerAdapter
from .provider import ProviderAdapter
from .types import ProviderKind, RunRecord


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
