from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


_ISOLATED_RUNTIME_DIR = Path(
    tempfile.mkdtemp(prefix="wiki-agent-test-runtime-")
)


def pytest_configure() -> None:
    """Set the test runtime before backend modules are imported."""

    os.environ["WIKI_AGENT_RUNTIME_DIR"] = str(_ISOLATED_RUNTIME_DIR)
    os.environ["WIKI_REBASE_TEST_MODE"] = "1"
    os.environ["WIKI_ACCOUNT_NOTICES_PATH"] = str(
        _ISOLATED_RUNTIME_DIR / "account-notices.json"
    )


@pytest.fixture(scope="session", autouse=True)
def isolate_rebase_bot_runtime():
    """Keep every backend test away from the operator runtime."""

    from backend.app import main

    main.AGENT_RUNTIME_DIR = _ISOLATED_RUNTIME_DIR
    yield
