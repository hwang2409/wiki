#!/usr/bin/env python3
"""Print the fingerprint used by the frozen backend runtime."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent_runtime.version import frozen_runtime_fingerprint  # noqa: E402


if len(sys.argv) != 2:
    raise SystemExit("usage: native_backend_fingerprint.py EXECUTABLE")

print(frozen_runtime_fingerprint(Path(sys.argv[1]).resolve()))
