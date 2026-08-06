#!/usr/bin/env python3
"""Print the fingerprint used by the frozen backend runtime."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Load version.py directly: importing it through the backend.app.agent_runtime
# package pulls the full runtime (pydantic, PIL, ...) which the plain build
# interpreter does not have.
_VERSION_PATH = ROOT / "backend" / "app" / "agent_runtime" / "version.py"
_spec = importlib.util.spec_from_file_location("_agent_runtime_version", _VERSION_PATH)
assert _spec is not None and _spec.loader is not None
_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_version)
frozen_runtime_fingerprint = _version.frozen_runtime_fingerprint


if len(sys.argv) != 2:
    raise SystemExit("usage: native_backend_fingerprint.py EXECUTABLE")

print(frozen_runtime_fingerprint(Path(sys.argv[1]).resolve()))
