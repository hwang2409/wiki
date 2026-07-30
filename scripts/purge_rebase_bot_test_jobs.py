#!/usr/bin/env python3
"""Remove leaked completed retry-test jobs from a rebase-bot store."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def purge(runtime_dir: Path) -> int:
    """Remove completed retry-test jobs with an empty outbox atomically."""

    os.environ["WIKI_AGENT_RUNTIME_DIR"] = str(runtime_dir)
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from backend.app.agent_runtime import rebase_durable

    with rebase_durable._state_lock():
        rebase_durable._load_durable_state()
        if rebase_durable._OUTBOX:
            raise RuntimeError("refusing to purge while the rebase-bot outbox is not empty")

        keys = [
            key
            for key, record in rebase_durable._DURABLE_JOBS.items()
            if record.get("status") == "completed"
            and str(record.get("expected_sha") or "").startswith("retry-test-")
        ]
        for key in keys:
            del rebase_durable._DURABLE_JOBS[key]
        if keys:
            rebase_durable._persist_durable_state()
        return len(keys)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    count = purge(args.runtime_dir.expanduser().resolve())
    print(f"purged {count} retry-test job(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
