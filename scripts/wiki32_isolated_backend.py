#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch an isolated wiki backend for WIKI-32 verification.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--status-dir", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--codex-sessions-dir", type=Path, required=True)
    parser.add_argument("--log-level", default="warning")
    return parser.parse_args()


def main_cli() -> None:
    args = parse_args()
    fixture_root = args.registry.parent.resolve()
    archive_dir = fixture_root / "archive"
    runtime_dir = fixture_root / "runtime"
    agent_tmp_dir = fixture_root / "tmp"
    vault_dir = fixture_root / "vault"
    claude_projects_dir = fixture_root / "claude-projects"
    account_home = fixture_root / "account-home"
    supervisor_socket = Path(
        os.environ.get("WIKI_SUPERVISOR_SOCKET_PATH")
        or runtime_dir / "supervisor.sock"
    )
    args.registry.parent.mkdir(parents=True, exist_ok=True)
    args.status_dir.mkdir(parents=True, exist_ok=True)
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    args.codex_sessions_dir.mkdir(parents=True, exist_ok=True)
    for directory in (
        archive_dir,
        runtime_dir,
        agent_tmp_dir,
        vault_dir,
        claude_projects_dir,
        account_home,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # Set every agent/auth/transcript path before importing the backend. This
    # harness must never inherit live registry, send-keys, auth, vault, or
    # provider-session powers from the developer environment.
    os.environ.update(
        {
            "WIKI_AGENT_REGISTRY_PATH": str(args.registry),
            "WIKI_AGENT_STATUS_DIR": str(args.status_dir),
            "WIKI_AGENT_ARCHIVE_DIR": str(archive_dir),
            "WIKI_AGENT_TMP_DIR": str(agent_tmp_dir),
            "WIKI_AGENT_VIEWED_PATH": str(fixture_root / "agent-viewed.json"),
            "WIKI_AGENT_DEPLOY_MARKER_PATH": str(fixture_root / "deploy-timestamp.txt"),
            "WIKI_MSG_QUEUE_PATH": str(args.queue),
            "WIKI_AGENT_RUNTIME_DIR": str(runtime_dir),
            "WIKI_KNOWLEDGE_DB_PATH": str(fixture_root / "knowledge.db"),
            "WIKI_SUPERVISOR_SOCKET_PATH": str(supervisor_socket),
            "WIKI_SUPERVISOR_AUTOSTART": "off",
            "WIKI_VAULT_DIR": str(vault_dir),
            "WIKI_CODEX_SESSIONS_DIR": str(args.codex_sessions_dir),
            "WIKI_CLAUDE_PROJECTS_DIR": str(claude_projects_dir),
            "WIKI_ACCOUNT_HOME_OVERRIDE": str(account_home),
            "WIKI_CODEX_AUTH_PATH": str(account_home / "codex" / "auth.json"),
            "WIKI_CODEX_ACCOUNTS_DIR": str(account_home / "codex-accounts"),
            "WIKI_ROTATION_LOG_PATH": str(account_home / "rotation.log"),
            "WIKI_ACCOUNT_WATCHDOG": "off",
            "WIKI_UI_STATE_PATH": str(fixture_root / "ui-state.json"),
            "CODEX_HOME": str(account_home / "codex"),
            "CLAUDE_CONFIG_DIR": str(account_home / "claude"),
            # WIKI-151: pin UI-state + token-cache paths under the fixture root
            # unconditionally so every harness consumer is isolated from the
            # developer's live ~/.wiki/ui-state.json (window layout, sidebar
            # tab, dashboard filters, workspaces) and ~/.wiki/token-cache.json.
            # Prior harness invocations that didn't set these explicitly
            # inherited Henry's live state and could overwrite it on the
            # debounced localStorage flush.
            "WIKI_UI_STATE_PATH": str(fixture_root / "ui-state.json"),
            "WIKI_TOKEN_CACHE_PATH": str(fixture_root / "token-cache.json"),
        }
    )

    from backend.app import main, transcripts, uistate

    uistate.UI_STATE_PATH = fixture_root / "ui-state.json"
    main.AGENT_REGISTRY_PATH = args.registry
    main.AGENT_STATUS_DIR = args.status_dir
    main.AGENT_ARCHIVE_DIR = archive_dir
    main.AGENT_TMP_DIR = agent_tmp_dir
    main.AGENT_VIEWED_PATH = fixture_root / "agent-viewed.json"
    main.AGENT_DEPLOY_MARKER_PATH = fixture_root / "deploy-timestamp.txt"
    main.AGENT_RUNTIME_DIR = runtime_dir
    main.AGENT_RUNS_DIR = runtime_dir / "runs"
    main.MSG_QUEUE_PATH = args.queue
    transcripts.CODEX_SESSIONS_DIR = args.codex_sessions_dir
    transcripts.CLAUDE_PROJECTS_DIR = claude_projects_dir

    uvicorn.run(main.app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main_cli()
