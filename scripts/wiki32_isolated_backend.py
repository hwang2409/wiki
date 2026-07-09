#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from backend.app import main, transcripts


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
    args.registry.parent.mkdir(parents=True, exist_ok=True)
    args.status_dir.mkdir(parents=True, exist_ok=True)
    args.queue.parent.mkdir(parents=True, exist_ok=True)
    args.codex_sessions_dir.mkdir(parents=True, exist_ok=True)

    main.AGENT_REGISTRY_PATH = args.registry
    main.AGENT_STATUS_DIR = args.status_dir
    main.MSG_QUEUE_PATH = args.queue
    transcripts.CODEX_SESSIONS_DIR = args.codex_sessions_dir

    uvicorn.run(main.app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main_cli()
