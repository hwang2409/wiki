from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def _runtime_fingerprint() -> str:
    """Identify the supervisor code loaded by this process."""

    digest = hashlib.sha256()
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        # Content identity survives the app bundle copying the executable to
        # its final path. It still changes when a detached old binary runs
        # after an app update, so the supervisor swap guard remains effective.
        digest.update(executable.read_bytes())
    else:
        runtime_dir = Path(__file__).resolve().parent
        sources = [
            *runtime_dir.glob("*.py"),
            *runtime_dir.parent.glob("knowledge*.py"),
            runtime_dir.parent / "wiki_artifacts.py",
        ]
        for path in sorted(sources):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# Capture this at import time. A detached frozen daemon can keep executing an
# old inode after Wiki.app replaces the bundle at the same filesystem path.
RUNTIME_FINGERPRINT = _runtime_fingerprint()

# Frozen (bundled Wiki.app) processes own the supervisor upgrade path; dev
# checkouts and worktrees must never swap-kill a live supervisor (WIKI-217).
RUNTIME_FROZEN = bool(getattr(sys, "frozen", False))
