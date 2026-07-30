from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_rebase_bot_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep every backend test away from the operator runtime."""

    runtime_dir = tmp_path / "agent-runtime"
    monkeypatch.setenv("WIKI_AGENT_RUNTIME_DIR", str(runtime_dir))

    from backend.app import main

    monkeypatch.setattr(main, "AGENT_RUNTIME_DIR", runtime_dir)
    yield
